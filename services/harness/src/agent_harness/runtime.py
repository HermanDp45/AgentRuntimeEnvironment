"""История сессий, запуски, подтверждения и события для gateway."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from agent_harness.clients import SandboxClient, json_request
from agent_harness.loop import SYSTEM, Engine, Stop
from agent_harness.tools import failure
from contracts.v0 import Cancelled, RunState


class Rejected(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


@dataclass
class Run:
    request: object
    events: list = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    result: object = None
    task: asyncio.Task | None = None
    pending: tuple | None = None
    decisions: dict = field(default_factory=dict)
    cancelling: bool = False
    started: bool = False

    def state(self):
        return RunState(
            version="0",
            type="run.state",
            session_id=self.request.session_id,
            run_id=self.request.run_id,
            state="finished"
            if self.result
            else ("waiting_approval" if self.pending else "running"),
            result=self.result,
            pending_operation_id=self.pending[0] if self.pending else None,
        )


@dataclass
class Session:
    request: object
    sandbox: SandboxClient
    history: list = field(default_factory=lambda: [{"role": "system", "content": SYSTEM}])
    runs: dict = field(default_factory=dict)
    active: Run | None = None
    closing: bool = False
    touched: float = field(default_factory=time.monotonic)


class Runtime:
    def __init__(self, model, sandbox_http, config, trace_dir):
        self.model, self.sandbox_http, self.config = model, sandbox_http, config
        self.sessions, self.runs = {}, {}
        self.lock = asyncio.Lock()
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.reaper = asyncio.create_task(self._reap())

    async def provision(self, request):
        async with self.lock:
            if old := self.sessions.get(request.session_id):
                if old.request != request or old.closing:
                    raise Rejected("session_conflict")
                return
            if len(self.sessions) >= self.config["max_sessions"]:
                raise Rejected("session_limit")
            payload = {
                **request.model_dump(mode="json"),
                "type": "provision.mount" if request.type == "session.create" else "provision",
            }
            try:
                response = await self.sandbox_http.post("/v0/provision", json=payload)
                response.raise_for_status()
            except httpx2.HTTPStatusError as error:
                try:
                    code = error.response.json().get("error", {}).get("code", "")
                except ValueError:
                    code = ""
                if code in ("workdir_image_missing", "workdir_unavailable"):
                    raise Rejected(code, 503) from None
                raise Stop(failure("transport", "upstream_unavailable")) from None
            except httpx2.HTTPError:
                raise Stop(failure("transport", "upstream_unavailable")) from None
            session = Session(request, SandboxClient(self.sandbox_http, request.session_id))
            if request.type == "session.create":
                session.history[0]["content"] += (
                    "\nShell commands can read /work. Use write_file for approved changes."
                )
            self.sessions[request.session_id] = session

    def session(self, identifier):
        session = self.sessions.get(identifier)
        if session is None or session.closing:
            raise Rejected("session_not_found", 404)
        if session.sandbox.uncertain:
            raise Rejected("sandbox_outcome_unknown")
        session.touched = time.monotonic()
        return session

    def run(self, identifier):
        if identifier not in self.runs:
            raise Rejected("run_not_found", 404)
        return self.runs[identifier]

    async def start(self, request):
        async with self.lock:
            if old := self.runs.get(request.run_id):
                if old.request != request:
                    raise Rejected("run_conflict")
                return old
            session = self.session(request.session_id)
            if session.active:
                raise Rejected("session_busy")
            if len(session.runs) >= self.config["max_runs_per_session"]:
                raise Rejected("run_limit")
            run = Run(request)
            self.runs[request.run_id] = session.runs[request.run_id] = run
            session.active = run
            run.task = asyncio.create_task(self._run(session, run))
            return run

    async def _run(self, session, run):
        run.started = True

        def emit(event, data):
            if len(run.events) >= 2048 and data.get("phase") in ("stdout", "stderr"):
                return
            item = {
                "version": "0",
                "type": "run.event",
                "session_id": str(run.request.session_id),
                "run_id": str(run.request.run_id),
                "sequence": len(run.events) + 1,
                "event": event,
                "data": data,
            }
            run.events.append(item)
            # Тексты, аргументы и diff остаются в RAM; на диск идут только метаданные.
            metadata = {
                key: value
                for key, value in data.items()
                if key
                in (
                    "phase",
                    "kind",
                    "operation_id",
                    "duration_s",
                    "usage",
                    "model",
                    "approved",
                    "status",
                    "exit_code",
                )
            }
            self._trace(run, event, metadata)
            run.changed.set()

        async def approve(identifier, data):
            future = asyncio.get_running_loop().create_future()
            run.pending = identifier, future
            emit("approval_required", {"operation_id": str(identifier), **data})
            try:
                return await future
            finally:
                run.pending = None

        started = time.monotonic()
        emit("status", {"phase": "started"})
        try:
            if run.cancelling:
                result = Cancelled(status="cancelled")
            else:
                result = await Engine(self.model, session.sandbox, self.config).run(
                    session.history, run.request.message, approve, emit
                )
        except Stop as stop:
            result = stop.result
        except Exception:
            result = failure("transport", "harness_failed")
        if session.sandbox.uncertain:
            result = failure("transport", "sandbox_outcome_unknown")
        run.result = result
        run.events.append(
            {
                "version": "0",
                "type": "run.result",
                "session_id": str(run.request.session_id),
                "run_id": str(run.request.run_id),
                "sequence": len(run.events) + 1,
                "result": result.model_dump(),
            }
        )
        self._trace(
            run, "result", {"status": result.status, "duration_s": time.monotonic() - started}
        )
        session.active = None
        session.touched = time.monotonic()
        run.changed.set()

    def _trace(self, run, event, data):
        record = {
            "trace_id": str(run.request.run_id),
            "session_id": str(run.request.session_id),
            "sequence": len(run.events),
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            **data,
        }
        path = self.trace_dir / f"{run.request.session_id}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logging.getLogger(__name__).warning("Trace metadata could not be written")

    def approve(self, body):
        run = self.run(body.run_id)
        if run.request.session_id != body.session_id:
            raise Rejected("session_mismatch")
        if body.operation_id in run.decisions:
            if run.decisions[body.operation_id] != body.approved:
                raise Rejected("approval_conflict")
            return run
        if (
            run.result
            or run.cancelling
            or run.pending is None
            or run.pending[0] != body.operation_id
        ):
            raise Rejected("approval_not_pending")
        run.decisions[body.operation_id] = body.approved
        self._trace(
            run, "approval", {"operation_id": str(body.operation_id), "approved": body.approved}
        )
        run.pending[1].set_result(body.approved)
        return run

    async def cancel(self, body):
        run = self.run(body.run_id)
        if run.request.session_id != body.session_id:
            raise Rejected("session_mismatch")
        if not run.cancelling and run.result is None:
            run.cancelling = True
            if run.started:
                run.task.cancel()
        await run.task
        return run

    async def collect(self, identifier):
        session = self.session(identifier)
        if session.active:
            raise Rejected("session_busy")
        return await json_request(
            self.sandbox_http,
            "POST",
            "/v0/collect",
            json={"version": "0", "type": "collect", "session_id": str(identifier)},
        )

    async def release(self, identifier):
        async with self.lock:
            session = self.sessions.get(identifier)
            if session is None:
                return
            session.closing = True
            if session.active:
                run = session.active
                if not run.cancelling:
                    run.cancelling = True
                    if run.started:
                        run.task.cancel()
                await run.task
            await json_request(
                self.sandbox_http,
                "POST",
                "/v0/release",
                json={"version": "0", "type": "release", "session_id": str(identifier)},
            )
            for identifier_run in session.runs:
                self.runs.pop(identifier_run, None)
            del self.sessions[identifier]
            (self.trace_dir / f"{identifier}.jsonl").unlink(missing_ok=True)

    async def _reap(self):
        while True:
            await asyncio.sleep(min(10, self.config["idle_s"]))
            for identifier, session in list(self.sessions.items()):
                if (
                    not session.active
                    and time.monotonic() - session.touched > self.config["idle_s"]
                ):
                    try:
                        await self.release(identifier)
                    except Stop:
                        pass

    async def close(self):
        self.reaper.cancel()
        await asyncio.gather(self.reaper, return_exceptions=True)
        for identifier in list(self.sessions):
            await self.release(identifier)
