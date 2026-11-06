"""Полный путь gateway → харнес → прокси → mock LLM → настоящий сандбокс."""

import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from contracts.v0 import ClientMessage

BASE = os.getenv("HARNESS_TEST_URL", "").rstrip("/")
TOKEN = "fixture-client-token"
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not BASE, reason="Use make up-test/test-agent"),
]


def http(path, body=None, token=TOKEN, headers=None):
    request = Request(
        BASE + path,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            **(headers or {}),
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            text = response.read().decode()
            return response.status, json.loads(
                text
            ) if response.headers.get_content_type() == "application/json" else text
    except HTTPError as error:
        return error.code, json.load(error)


def message(kind, **fields):
    return {"version": "0", "type": kind, **fields}


@pytest.fixture
def session():
    identifier = str(uuid4())
    body = message("session.fixture", session_id=identifier, files={"note.txt": "original"})
    assert http("/v0/sessions", body)[0] == 200
    assert http("/v0/sessions", body)[0] == 200
    yield identifier
    assert http("/v0/close", message("session.close", session_id=identifier))[0] == 200


def start(session, text):
    body = message("task", session_id=session, run_id=str(uuid4()), message=text)
    assert http("/v0/tasks", body)[0] == 202
    return body


def state(body):
    status, result = http(f"/v0/runs/{body['run_id']}")
    assert status == 200
    TypeAdapter(ClientMessage).validate_python(result)
    return result


def wait(body, predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = state(body)
        if predicate(current):
            return current
        time.sleep(0.03)
    pytest.fail("Run did not reach the expected state")


def approval(body, current, approved):
    return message(
        "approval",
        session_id=body["session_id"],
        run_id=body["run_id"],
        operation_id=current["pending_operation_id"],
        approved=approved,
    )


def test_read_write_approvals_command_error_and_resume(session):
    body = start(session, "test:edit")
    current = wait(body, lambda item: item["state"] == "waiting_approval")
    # Пока пользователь не ответил, файл действительно не изменён.
    assert http("/v0/tasks", body)[0] == 202
    assert http("/v0/tasks", body | {"message": "changed"})[0] == 409
    decision = approval(body, current, True)
    assert http("/v0/approvals", decision)[0] == 200
    assert http("/v0/approvals", decision)[0] == 200
    assert http("/v0/approvals", decision | {"approved": False})[0] == 409
    current = wait(
        body,
        lambda item: (
            item["state"] == "waiting_approval"
            and item["pending_operation_id"] != decision["operation_id"]
        ),
    )
    assert http("/v0/approvals", approval(body, current, True))[0] == 200
    finished = wait(body, lambda item: item["state"] == "finished")
    assert finished["result"]["status"] == "ok"
    assert finished["result"]["value"]["model_calls"] == 4
    assert "кодом 1" in finished["result"]["value"]["message"]
    status, collection = http(f"/v0/sessions/{session}/files")
    assert status == 200 and collection["files"]["note.txt"]["content"] == "changed"
    stream = http(f"/v0/runs/{body['run_id']}/events")[1]
    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
    assert sum(event["type"] == "run.result" for event in events) == 1
    for event in events:
        TypeAdapter(ClientMessage).validate_python(event)
    previews = [event["data"] for event in events if event.get("event") == "approval_required"]
    assert len(previews) == 2 and "-original" in previews[0]["diff"]
    assert "fixture-provider-secret" not in stream
    resumed = http(f"/v0/runs/{body['run_id']}/events", headers={"Last-Event-ID": "1"})[1]
    resumed_events = [
        json.loads(line[6:]) for line in resumed.splitlines() if line.startswith("data: ")
    ]
    assert all(event["sequence"] > 1 for event in resumed_events)
    assert sum(event["type"] == "run.result" for event in resumed_events) == 1
    trace = http(f"/v0/sessions/{session}/trace")[1]
    records = [json.loads(line) for line in trace.splitlines()]
    assert any(record.get("usage") for record in records)
    assert any(record.get("kind") == "RunCommand" and "duration_s" in record for record in records)
    assert (
        "original" not in trace
        and "changed" not in trace
        and "fixture-provider-secret" not in trace
    )
    assert "run.result" not in http(f"/v0/runs/{body['run_id']}/events?after={len(events)}")[1]
    next_task = start(session, "test:history")
    result = wait(next_task, lambda item: item["state"] == "finished")
    assert "test:edit" in result["result"]["value"]["message"]


def test_denial_and_cancel_while_waiting_do_not_execute(session):
    body = start(session, "test:deny")
    current = wait(body, lambda item: item["state"] == "waiting_approval")
    assert http("/v0/approvals", approval(body, current, False))[0] == 200
    assert wait(body, lambda item: item["state"] == "finished")["result"]["status"] == "ok"
    assert "forbidden.txt" not in http(f"/v0/sessions/{session}/files")[1]["files"]
    body = start(session, "test:deny")
    current = wait(body, lambda item: item["state"] == "waiting_approval")
    cancel = message("run.cancel", session_id=session, run_id=body["run_id"])
    assert http("/v0/cancel", cancel)[1]["result"]["status"] == "cancelled"
    assert http("/v0/approvals", approval(body, current, True))[0] == 409
    assert "forbidden.txt" not in http(f"/v0/sessions/{session}/files")[1]["files"]


def test_cancel_running_command_preserves_completed_file(session):
    body = start(session, "test:cancel")
    current = wait(body, lambda item: item["state"] == "waiting_approval")
    assert http("/v0/approvals", approval(body, current, True))[0] == 200
    request = Request(
        BASE + f"/v0/runs/{body['run_id']}/events", headers={"Authorization": "Bearer " + TOKEN}
    )
    with urlopen(request, timeout=10) as stream:
        while line := stream.readline():
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                if (
                    event.get("data", {}).get("phase") == "stdout"
                    and "ready" in event["data"]["text"]
                ):
                    break
        else:
            pytest.fail("Command never started")
    cancel = message("run.cancel", session_id=session, run_id=body["run_id"])
    assert http("/v0/cancel", cancel)[1]["result"]["status"] == "cancelled"
    assert http("/v0/cancel", cancel)[0] == 200
    files = http(f"/v0/sessions/{session}/files")[1]["files"]
    assert files["saved.txt"]["content"] == "saved"
    following = start(session, "test:history")
    assert wait(following, lambda item: item["state"] == "finished")["result"]["status"] == "ok"


def test_auth_invalid_tool_and_session_isolation(session):
    assert http(f"/v0/sessions/{session}/files", token="wrong")[0] == 401
    body = start(session, "test:invalid")
    assert wait(body, lambda item: item["state"] == "finished")["result"]["status"] == "ok"
    other = str(uuid4())
    assert (
        http(
            "/v0/sessions",
            message("session.fixture", session_id=other, files={"note.txt": "other"}),
        )[0]
        == 200
    )
    try:
        assert http(f"/v0/sessions/{other}/files")[1]["files"]["note.txt"]["content"] == "other"
        assert (
            http(f"/v0/sessions/{session}/files")[1]["files"]["note.txt"]["content"] == "original"
        )
    finally:
        assert http("/v0/close", message("session.close", session_id=other))[0] == 200


def test_console_client_shows_diff_and_requests_each_approval():
    process = subprocess.run(
        [sys.executable, "-m", "client.cli", "--url", BASE, "--task", "test:edit"],
        input="y\ny\n",
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "CLIENT_TOKEN": TOKEN},
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert process.stdout.count("Разрешить?") == 2
    assert "-original" in process.stdout and "+changed" in process.stdout
    assert "changed" in process.stdout and "Итог: ok" in process.stdout
