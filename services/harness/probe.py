"""Один реальный read-only сценарий на синтетическом файле, до трёх ответов модели."""

import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx2


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: probe.py <report.json>")
    session, run = str(uuid4()), str(uuid4())
    report = {"fixture": {"note.txt": "PORT=7319"}, "events": [], "passed": False}
    with httpx2.Client(
        base_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080"),
        headers={"Authorization": "Bearer " + os.getenv("CLIENT_TOKEN", "local-dev-token")},
        timeout=20,
        trust_env=False,
        follow_redirects=False,
    ) as client:

        def post(path, kind, **data):
            response = client.post(path, json={"version": "0", "type": kind, **data})
            response.raise_for_status()
            return response.json()

        created, started = False, time.monotonic()
        try:
            post("/v0/sessions", "session.fixture", session_id=session, files=report["fixture"])
            created = True
            post(
                "/v0/tasks",
                "task",
                session_id=session,
                run_id=run,
                message=(
                    "Прочитай note.txt инструментом read_file. "
                    "Ответь только значением PORT из файла. "
                    "Не изменяй файлы, не запускай команды, не угадывай значение."
                ),
            )
            with client.stream("GET", f"/v0/runs/{run}/events") as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if time.monotonic() - started > 150:
                        post("/v0/cancel", "run.cancel", session_id=session, run_id=run)
                        break
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    report["events"].append(event)
                    if event["type"] == "run.result":
                        report["result"] = event["result"]
                        break
                    if event.get("event") == "approval_required":
                        post(
                            "/v0/approvals",
                            "approval",
                            session_id=session,
                            run_id=run,
                            operation_id=event["data"]["operation_id"],
                            approved=False,
                        )
                    if event.get("data", {}).get("model_call", 0) > 3:
                        post("/v0/cancel", "run.cancel", session_id=session, run_id=run)
                        break
            result = report.get("result", {})
            report["passed"] = (
                result.get("status") == "ok"
                and result.get("value", {}).get("message", "").strip() == "7319"
                and any(
                    event.get("data", {}).get("kind") == "ReadFile"
                    and event["data"].get("phase") == "finished"
                    for event in report["events"]
                )
            )
        except httpx2.HTTPError as error:
            report["error"] = type(error).__name__
        finally:
            if created:
                trace = client.get(f"/v0/sessions/{session}/trace")
                if trace.status_code == 200:
                    report["trace"] = [json.loads(line) for line in trace.text.splitlines()]
                post("/v0/close", "session.close", session_id=session)
            report["seconds"] = round(time.monotonic() - started, 3)
            destination = Path(sys.argv[1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "seconds": report["seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
