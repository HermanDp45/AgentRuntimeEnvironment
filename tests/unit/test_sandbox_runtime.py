import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from contracts.v0 import Exec, Provision, Success
from sandbox_manager.app import create_app
from sandbox_manager.runtime import Rejected, Runtime


class Backend:
    def __init__(self):
        self.calls, self.removed = 0, []
        self.started = threading.Event()

    def health(self):
        pass

    def provision(self, request):
        return request.session_id

    def execute(self, container, request, output, gate, cancelled, backup_dir):
        self.calls += 1
        self.started.set()
        if request.operation.command == "wait":
            while not cancelled():
                time.sleep(0.005)
        if request.operation.command == "broken":
            raise OSError("transport broke")
        output("stdout", "ok")
        return Success(status="ok")

    def cancel(self, container):
        pass

    def release(self, container):
        self.removed.append(container)


@pytest.fixture
def runtime(tmp_path):
    backend = Backend()
    runtime = Runtime(
        backend,
        {
            "max_sessions": 2,
            "max_operations": 8,
            "file_bytes": 65536,
            "timeout_s": 2,
            "idle_s": 30,
        },
        tmp_path,
    )
    yield runtime
    runtime.close()


def provision(runtime):
    request = Provision(version="0", type="provision", session_id=uuid4())
    runtime.provision(request)
    runtime.provision(request)
    return request


def execute(runtime, session, command="ok", timeout_s=2):
    request = Exec(
        version="0",
        type="exec",
        session_id=session.session_id,
        operation_id=uuid4(),
        operation={"kind": "RunCommand", "command": command},
        timeout_s=timeout_s,
    )
    return runtime.execute(request)


def test_repeated_request_is_observation_not_execution(runtime):
    session = provision(runtime)
    operation = execute(runtime, session)
    assert operation.done.wait(1)
    assert runtime.execute(operation.request) is operation
    assert runtime.backend.calls == 1
    assert sum(event["type"] == "result" for event in operation.events) == 1
    with pytest.raises(Rejected) as error:
        runtime.execute(operation.request.model_copy(update={"timeout_s": 1}))
    assert error.value.code == "operation_conflict"


def test_cancel_and_timeout_are_terminal_and_session_is_reusable(runtime):
    session = provision(runtime)
    operation = execute(runtime, session, "wait")
    assert runtime.backend.started.wait(1)
    with pytest.raises(Rejected) as error:
        execute(runtime, session)
    assert error.value.code == "session_busy"
    runtime.cancel(operation.request.operation_id)
    runtime.cancel(operation.request.operation_id)
    assert operation.state().result.status == "cancelled"
    timed = execute(runtime, session, "wait", timeout_s=0.02)
    assert timed.done.wait(1)
    assert timed.state().result.error.kind == "timeout"
    assert sum(event["type"] == "result" for event in timed.events) == 1


def test_transport_failure_does_not_retry(runtime):
    operation = execute(runtime, provision(runtime), "broken")
    assert operation.done.wait(1)
    assert operation.state().result.error.kind == "transport"
    assert runtime.backend.calls == 1


def test_failed_cancel_does_not_claim_processes_were_stopped(runtime, monkeypatch):
    operation = execute(runtime, provision(runtime), "wait")
    assert runtime.backend.started.wait(1)

    def unavailable(container):
        raise OSError("Docker unavailable")

    monkeypatch.setattr(runtime.backend, "cancel", unavailable)
    runtime.cancel(operation.request.operation_id)
    assert operation.state().result.error.kind == "transport"


def test_sse_resume_and_validation_do_not_echo_file_contents(runtime):
    session = provision(runtime)
    operation = execute(runtime, session)
    assert operation.done.wait(1)
    with TestClient(create_app(runtime)) as client:
        url = f"/v0/operations/{operation.request.operation_id}/events"
        stream = client.get(url, headers={"Last-Event-ID": "1"})
        assert "event: started" not in stream.text
        assert stream.text.count("event: result") == 1
        assert client.get(url, params={"after": len(operation.events)}).text == ""
        body = session.model_dump(mode="json") | {"files": {"../private": "secret-content"}}
        response = client.post("/v0/provision", json=body)
        assert response.status_code == 422
        assert "secret-content" not in response.text


def test_idle_session_is_removed(tmp_path):
    runtime = Runtime(Backend(), {"max_sessions": 2, "file_bytes": 65536, "idle_s": 0.02}, tmp_path)
    session = provision(runtime)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and session.session_id not in runtime.backend.removed:
        time.sleep(0.01)
    assert session.session_id in runtime.backend.removed
    runtime.release(session.session_id)
    runtime.close()
