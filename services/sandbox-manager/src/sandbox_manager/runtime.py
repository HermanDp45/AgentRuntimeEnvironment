"""Сессии, идемпотентность, события и остановка операций."""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from contracts.v0 import Cancelled, Failed, OperationState


class Rejected(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


@dataclass
class Operation:
    request: object
    condition: threading.Condition = field(default_factory=threading.Condition)
    done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    events: list = field(default_factory=list)
    result: object = None
    stop_reason: str = "cancelled"
    output_remaining: int = 65536

    def emit(self, kind, **data):
        with self.condition:
            if self.result is not None:
                return
            if kind in ("stdout", "stderr"):
                clipped = data["text"].encode("utf-8")[: self.output_remaining]
                self.output_remaining -= len(clipped)
                if not clipped:
                    return
                data["text"] = clipped.decode("utf-8", errors="replace")
            event = {
                "version": "0",
                "type": kind,
                "session_id": str(self.request.session_id),
                "operation_id": str(self.request.operation_id),
                "sequence": len(self.events) + 1,
                **data,
            }
            self.events.append(event)
            if kind == "result":
                self.result = data["result"]
            self.condition.notify_all()

    def state(self):
        with self.condition:
            return OperationState(
                version="0",
                type="operation",
                session_id=self.request.session_id,
                operation_id=self.request.operation_id,
                state="finished" if self.result else "running",
                result=self.result,
            )


@dataclass
class Session:
    request: object
    container: object
    gate: threading.RLock = field(default_factory=threading.RLock)
    busy: Operation | None = None
    operations: dict = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)
    closing: bool = False


def failure(kind, code):
    return Failed(
        status="error", error={"kind": kind, "code": code, "message": code.replace("_", " ")}
    )


class Runtime:
    def __init__(self, backend, config, state_dir):
        self.backend, self.config = backend, config
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.sessions, self.operations = {}, {}
        self.closed = threading.Event()
        self.reaper = threading.Thread(target=self._reap, daemon=True)
        self.reaper.start()

    def provision(self, request):
        with self.lock:
            if session := self.sessions.get(request.session_id):
                if session.request != request or session.closing:
                    raise Rejected("session_conflict")
                session.touched = time.monotonic()
                return
            if len(self.sessions) >= self.config["max_sessions"]:
                raise Rejected("session_limit")
            for content in request.files.values():
                if len(content.encode("utf-8")) > self.config["file_bytes"]:
                    raise Rejected("file_too_large", 422)
            container = self.backend.provision(request)
            self.sessions[request.session_id] = Session(request, container)

    def session(self, identifier):
        session = self.sessions.get(identifier)
        if session is None or session.closing:
            raise Rejected("session_not_found", 404)
        session.touched = time.monotonic()
        return session

    def operation(self, identifier):
        with self.lock:
            if identifier not in self.operations:
                raise Rejected("operation_not_found", 404)
            return self.operations[identifier]

    def execute(self, request):
        with self.lock:
            if old := self.operations.get(request.operation_id):
                if old.request != request:
                    raise Rejected("operation_conflict")
                return old
            session = self.session(request.session_id)
            if session.busy:
                raise Rejected("session_busy")
            if len(session.operations) >= self.config["max_operations"]:
                raise Rejected("operation_limit")
            operation = Operation(request, output_remaining=self.config.get("output_bytes", 65536))
            session.busy = operation
            session.operations[request.operation_id] = operation
            self.operations[request.operation_id] = operation
            threading.Thread(target=self._run, args=(session, operation), daemon=True).start()
            return operation

    def _run(self, session, operation):
        request = operation.request
        timer = threading.Timer(
            min(request.timeout_s, self.config["timeout_s"]),
            self._stop,
            args=(session, operation, "timeout"),
        )
        operation.emit("started")
        timer.start()
        started = time.monotonic()
        try:
            if session.request.read_only and request.operation.kind == "WriteFile":
                result = failure("sandbox", "read_only")
            else:
                result = self.backend.execute(
                    session.container,
                    request,
                    lambda kind, text: operation.emit(kind, text=text),
                    session.gate,
                    operation.stop.is_set,
                    self.state_dir,
                )
        except Exception:
            result = failure("transport", "sandbox_unavailable")
        finally:
            timer.cancel()
        with session.gate:
            if operation.stop.is_set():
                result = (
                    Cancelled(status="cancelled")
                    if operation.stop_reason == "cancelled"
                    else (
                        failure("timeout", "deadline_exceeded")
                        if operation.stop_reason == "timeout"
                        else failure("transport", "cancel_failed")
                    )
                )
            result = result or failure("sandbox", "missing_result")
            operation.emit("result", result=result.model_dump())
        with self.lock:
            session.busy = None
            session.touched = time.monotonic()
            # Только метаданные: ни текста команды, ни содержимого файлов.
            try:
                trace_path = self.state_dir / "operations.jsonl"
                if trace_path.exists() and trace_path.stat().st_size > 1024 * 1024:
                    trace_path.unlink()
                with trace_path.open("a", encoding="utf-8") as trace:
                    trace.write(
                        json.dumps(
                            {
                                "session_id": str(request.session_id),
                                "operation_id": str(request.operation_id),
                                "kind": request.operation.kind,
                                "duration_s": round(time.monotonic() - started, 6),
                                "status": result.status,
                            }
                        )
                        + "\n"
                    )
            except OSError:
                logging.getLogger(__name__).warning("Operation metadata could not be saved")
            finally:
                operation.done.set()

    def _stop(self, session, operation, reason):
        with session.gate:
            if operation.result is not None:
                return
            operation.stop_reason = reason
            operation.stop.set()
            try:
                self.backend.cancel(session.container)
            except Exception:
                operation.stop_reason = "cancel_failed"

    def cancel(self, identifier):
        with self.lock:
            operation = self.operation(identifier)
            session = self.sessions[operation.request.session_id]
        self._stop(session, operation, "cancelled")
        if not operation.done.wait(15):
            raise Rejected("cancel_pending", 503)
        return operation

    def collect(self, identifier):
        with self.lock:
            session = self.session(identifier)
            if session.busy:
                raise Rejected("session_busy")
            result = self.backend.collect(session.container)
            if result.status != "ok":
                raise Rejected(result.error.code, 422)
            return result.value["files"]

    def release(self, identifier):
        with self.lock:
            session = self.sessions.get(identifier)
            if session is None:
                return
            session.closing = True
            operation = session.busy
        if operation:
            self._stop(session, operation, "cancelled")
            if not operation.done.wait(15):
                raise Rejected("cancel_pending", 503)
        with self.lock:
            if identifier not in self.sessions:
                return
            self.backend.release(session.container)
            for operation_id in session.operations:
                self.operations.pop(operation_id, None)
            del self.sessions[identifier]

    def _reap(self):
        while not self.closed.wait(min(10, self.config["idle_s"])):
            with self.lock:
                idle = [
                    key
                    for key, session in self.sessions.items()
                    if not session.busy
                    and time.monotonic() - session.touched >= self.config["idle_s"]
                ]
            for key in idle:
                try:
                    # Повторная проверка под lock: новая задача могла оживить сессию.
                    with self.lock:
                        session = self.sessions.get(key)
                        if (
                            session
                            and not session.busy
                            and time.monotonic() - session.touched >= self.config["idle_s"]
                        ):
                            self.release(key)
                except Exception:
                    pass  # Повтор очистки в следующем проходе; health обнаружит недоступный Docker.

    def close(self):
        self.closed.set()
        self.reaper.join(timeout=2)
        for identifier in list(self.sessions):
            self.release(identifier)
