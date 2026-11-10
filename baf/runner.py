"""Первые Б1–Б5 через gateway v0 с отдельным чекером для каждой задачи."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx2
import yaml

from client import api
from contracts.paths import safe_path
from contracts.v0 import Collection

ROOT = Path(__file__).resolve().parent
TASKS = {path.name.split("-")[0]: path for path in sorted(ROOT.glob("B[1-5]-*/B[1-5].1-*"))}


def fixture_files(directory):
    files = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in fixtures")
        if path.is_file():
            files[safe_path(path.relative_to(directory).as_posix())] = path.read_bytes().decode()
    return files


def check_files(checker, files):
    with tempfile.TemporaryDirectory(prefix="are-baf-") as directory:
        for name, data in files.items():
            path = Path(directory) / safe_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data.content.encode())
        result = subprocess.run(
            [sys.executable, str(checker), directory],
            capture_output=True,
            timeout=10,
            check=True,
        )
    verdict = json.loads(result.stdout)
    if (
        not isinstance(verdict, dict)
        or not isinstance(verdict.get("pass"), bool)
        or not isinstance(verdict.get("reason"), str)
    ):
        raise ValueError("Invalid checker verdict")
    return {"pass": verdict["pass"], "reason": verdict["reason"]}


def run_task(client, directory):
    task = yaml.safe_load((directory / "task.yaml").read_text())
    files = fixture_files(directory / "fixture")
    session_id, run_id = str(uuid4()), str(uuid4())
    run = {"session_id": session_id, "run_id": run_id}
    row = {
        "task_id": task["id"],
        "repeat": 1,
        "pass": False,
        "reason": "Нет финального результата",
        "outcome": "stream_incomplete",
        "tokens_in": 0,
        "tokens_out": 0,
        "model": None,
        "run_id": run_id,
        "approvals": [],
    }
    started = time.monotonic()
    deadline = started + task["timeout_s"]
    approved_commands = set()
    command_ok = False
    try:
        api.post(client, "/v0/sessions", "session.fixture", session_id=session_id, files=files)
        api.post(client, "/v0/tasks", "task", **run, message="\n".join(task["messages"]))
        with closing(
            api.events(client, run_id, stopped=lambda: time.monotonic() >= deadline)
        ) as events:
            for event in events:
                if event["type"] == "run.result":
                    result = event["result"]
                    error = result.get("error") or {}
                    row["outcome"] = error.get("kind", result["status"])
                    row["reason"] = error.get("code", result["status"])
                    break
                data = event["data"]
                if event["event"] == "approval_required":
                    operation = data["operation"]
                    kind, identifier = operation["kind"], data["operation_id"]
                    approved = (
                        kind == "WriteFile" and operation["path"] == task["answer_file"]
                    ) or (kind == "RunCommand" and operation["command"] == task.get("command"))
                    api.post(
                        client,
                        "/v0/approvals",
                        "approval",
                        **run,
                        operation_id=identifier,
                        approved=approved,
                    )
                    row["approvals"].append({"kind": kind, "approved": approved})
                    if kind == "RunCommand" and approved:
                        approved_commands.add(identifier)
                elif data.get("phase") == "model_reply":
                    usage = data["usage"]
                    row["tokens_in"] += usage.get("prompt_tokens") or 0
                    row["tokens_out"] += usage.get("completion_tokens") or 0
                    row["model"] = data["model"]
                elif data.get("phase") == "executed":
                    command_ok |= (
                        data["operation_id"] in approved_commands
                        and data["status"] == "ok"
                        and data["exit_code"] == 0
                    )
            else:
                if time.monotonic() >= deadline:
                    row.update(outcome="timeout", reason="Истёк таймаут задачи")
        if row["outcome"] == "ok":
            response = client.get(f"/v0/sessions/{session_id}/files")
            response.raise_for_status()
            collection = Collection.model_validate(response.json())
            row.update(check_files(directory / "check.py", collection.files))
            if row["pass"] and task.get("command") and not command_ok:
                row.update({"pass": False, "reason": "Требуемая команда не завершилась успешно"})
            row["outcome"] = "passed" if row["pass"] else "check_failed"
    except httpx2.HTTPStatusError as error:
        row.update(outcome="http_error", reason=f"HTTP {error.response.status_code}")
    except httpx2.HTTPError:
        row.update(outcome="transport", reason="HTTP-запрос прерван")
    except (subprocess.SubprocessError, ValueError):
        row.update(outcome="runner_error", reason="Некорректный ответ API или чекера")
    finally:
        # close идемпотентен и отменяет активную задачу, даже если ответ POST потерялся.
        try:
            api.post(client, "/v0/close", "session.close", session_id=session_id)
        except httpx2.HTTPError:
            row.update(
                {"pass": False, "outcome": "cleanup_error", "reason": "Закрытие не подтверждено"}
            )
    row["seconds"] = round(time.monotonic() - started, 3)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output or Path(f"results/baf/early/{stamp}.jsonl")
    output.parent.mkdir(parents=True, exist_ok=True)
    passed = True
    with (
        output.open("x", encoding="utf-8") as results,
        api.connect(args.url, os.getenv("CLIENT_TOKEN", "local-dev-token")) as client,
    ):
        for identifier in args.task or TASKS:
            row = run_task(client, TASKS[identifier])
            results.write(json.dumps(row, ensure_ascii=False) + "\n")
            results.flush()
            print(f"{identifier}: {row['outcome']} ({row['reason']})", flush=True)
            passed &= row["pass"]
    print(f"Результаты в {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
