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
