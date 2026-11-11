import json

import httpx2
import pytest

from baf.runner import TASKS, run_task


@pytest.mark.parametrize("command_ran, expected", [(True, "passed"), (False, "check_failed")])
def test_baf_requires_both_checked_file_and_successful_command(command_ran, expected):
    decisions = {}
    requests = []

    def handle(request):
        path = request.url.path
        requests.append(path)
        if path == "/v0/approvals":
            approval = json.loads(request.content)
            decisions[approval["operation_id"]] = approval["approved"]
        elif path.endswith("/events"):
            events = [
                {
                    "type": "run.event",
                    "event": "approval_required",
                    "data": {"operation_id": identifier, "operation": operation},
                }
                for identifier, operation in (
                    ("other-command", {"kind": "RunCommand", "command": "touch unrelated.txt"}),
                    ("other-write", {"kind": "WriteFile", "path": "stats.py"}),
                    (
                        "expected-command",
                        {"kind": "RunCommand", "command": "python stats.py data.csv"},
                    ),
                    ("expected-write", {"kind": "WriteFile", "path": "answer.txt"}),
                )
            ]
            if command_ran:
                events.append(
                    {
                        "type": "run.event",
                        "event": "tool",
                        "data": {
                            "phase": "executed",
                            "operation_id": "expected-command",
                            "status": "ok",
                            "exit_code": 0,
                        },
                    }
                )
            events.append({"type": "run.result", "result": {"status": "ok"}})
            return httpx2.Response(
                200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events)
            )
        elif path.endswith("/files"):
            return httpx2.Response(
                200,
                json={
                    "version": "0",
                    "type": "collection",
                    "session_id": path.split("/")[3],
                    "files": {"answer.txt": {"content": "17\n", "sha256": "a" * 64}},
                },
            )
        return httpx2.Response(200, json={})

    with httpx2.Client(base_url="http://test", transport=httpx2.MockTransport(handle)) as client:
        row = run_task(client, TASKS["B5.1"])

    assert decisions == {
        "other-command": False,
        "other-write": False,
        "expected-command": True,
        "expected-write": True,
    }
    assert row["outcome"] == expected
    assert row["pass"] is command_ran
    assert requests[-1] == "/v0/close"


def test_lost_task_response_does_not_repost_and_closes_session():
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/v0/tasks":
            raise httpx2.ReadTimeout("lost response", request=request)
        return httpx2.Response(200, json={})

    with httpx2.Client(base_url="http://test", transport=httpx2.MockTransport(handle)) as client:
        row = run_task(client, TASKS["B1.1"])

    assert row["pass"] is False and row["outcome"] == "transport"
    assert requests == ["/v0/sessions", "/v0/tasks", "/v0/close"]


def test_hidden_validation_transport_failure_cannot_pass(monkeypatch):
    def handle(request):
        if request.url.path.endswith("/events"):
            return httpx2.Response(
                200, text='data: {"type":"run.result","result":{"status":"ok"}}\n\n'
            )
        if request.url.path.endswith("/files"):
            return httpx2.Response(
                200,
                json={
                    "version": "0",
                    "type": "collection",
                    "session_id": request.url.path.split("/")[3],
                    "files": {
                        "slug.py": {
                            "content": "def slugify(text): return text\n",
                            "sha256": "a" * 64,
                        }
                    },
                },
            )
        return httpx2.Response(200, json={})

    def unavailable(*args):
        raise httpx2.ReadTimeout("validation unavailable")

    monkeypatch.setattr("baf.runner.validate_code", unavailable)
    with httpx2.Client(base_url="http://test", transport=httpx2.MockTransport(handle)) as client:
        row = run_task(client, TASKS["B6.1"])
    assert row["outcome"] == "transport"
    assert row["pass"] is False


def test_malformed_close_still_stops_mount_export(monkeypatch, tmp_path):
    from contextlib import contextmanager

    stopped = []

    @contextmanager
    def workspace(source):
        try:
            yield tmp_path, {"port": 12345, "export_token": "x" * 43}
        finally:
            stopped.append(True)

    def handle(request):
        if request.url.path.endswith("/events"):
            return httpx2.Response(
                200, text='data: {"type":"run.result","result":{"status":"error"}}\n\n'
            )
        if request.url.path == "/v0/close":
            return httpx2.Response(200, text="broken JSON")
        return httpx2.Response(200, json={})

    monkeypatch.setattr("baf.runner.mounted_fixture", workspace)
    with httpx2.Client(base_url="http://test", transport=httpx2.MockTransport(handle)) as client:
        row = run_task(client, TASKS["B6.1"], mount=True)
    assert row["outcome"] == "cleanup_error" and row["pass"] is False
    assert stopped == [True]
