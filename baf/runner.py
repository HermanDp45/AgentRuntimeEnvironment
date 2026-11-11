"""БАФ через gateway: отдельные сессии, разрешённые операции и внешние чекеры."""

import argparse
import ast
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

import httpx2
import yaml

from baf.workspace import mounted_fixture
from client import api
from contracts.paths import safe_path
from contracts.v0 import Collection, FileData

ROOT = Path(__file__).resolve().parent
TASKS = {path.parent.name.split("-")[0]: path.parent for path in sorted(ROOT.glob("*/*/task.yaml"))}
SMOKE = [f"B{n}.1" for n in range(1, 9)] + ["B9.1", "B9.2", "B9.3"]


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
        return run_checker(checker, directory)


def run_checker(checker, directory):
    result = subprocess.run(
        [sys.executable, str(checker), str(directory)],
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


def validate_code(files, tests):
    """Скрытые тесты исполняются через существующий протокол изолированного менеджера."""
    session = str(uuid4())
    report = f"report-{uuid4().hex}.xml"
    test_source = tests.read_text()
    expected = {
        node.name
        for node in ast.parse(test_source).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    if not expected:
        raise ValueError("No hidden tests")
    with httpx2.Client(
        base_url=os.getenv("SANDBOX_TEST_URL", "http://127.0.0.1:8081"),
        timeout=20,
        trust_env=False,
        follow_redirects=False,
    ) as sandbox:
        try:
            api.post(
                sandbox,
                "/v0/provision",
                "provision",
                session_id=session,
                files={
                    **{name: data.content for name, data in files.items()},
                    "test_hidden.py": test_source,
                },
            )
            operation = str(uuid4())
            api.post(
                sandbox,
                "/v0/exec",
                "exec",
                session_id=session,
                operation_id=operation,
                timeout_s=5,
                operation={
                    "kind": "RunCommand",
                    "command": f"python -m pytest -q test_hidden.py --junitxml={report}",
                },
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                response = sandbox.get(f"/v0/operations/{operation}")
                response.raise_for_status()
                state = response.json()
                if state["state"] == "finished":
                    if state["result"]["status"] != "ok":
                        return False
                    collected = api.post(sandbox, "/v0/collect", "collect", session_id=session)
                    evidence = Collection.model_validate(collected).files.get(report)
                    if evidence is None:
                        return False
                    try:
                        root = ElementTree.fromstring(evidence.content)
                    except ElementTree.ParseError:
                        return False
                    cases = root.findall(".//testcase")
                    # exit(0) при импорте не означает, что pytest выполнил проверки.
                    return (
                        len(cases) == len(expected)
                        and {case.get("name") for case in cases} == expected
                        and all(case.get("classname") == "test_hidden" for case in cases)
                        and all(
                            case.find(tag) is None
                            for case in cases
                            for tag in ("failure", "error", "skipped")
                        )
                    )
                time.sleep(0.05)
            return False
        finally:
            api.post(sandbox, "/v0/release", "release", session_id=session)


def run_task(client, directory, *, mount=False):
    task = yaml.safe_load((directory / "task.yaml").read_text())
    if task.get("requires_mount") and not mount:
        raise ValueError(f"{task['id']} requires --mount")
    files = {} if mount else fixture_files(directory / "fixture")
    outputs = task.get("answer_files", [task["answer_file"]])
    commands = task.get("commands", {task["command"]: 0} if task.get("command") else {})
    hidden_files = task.get("hidden_files", ["slug.py"])
    resources = ExitStack()
    local = None
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
    approved_commands = {}
    completed_commands = set()
    try:
        if mount:
            local, payload = resources.enter_context(mounted_fixture(directory / "fixture"))
            api.post(client, "/v0/sessions", "session.create", session_id=session_id, **payload)
        else:
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
                    approved = (kind == "WriteFile" and operation["path"] in outputs) or (
                        kind == "RunCommand" and operation["command"] in commands
                    )
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
                        approved_commands[identifier] = operation["command"]
                elif data.get("phase") == "model_reply":
                    usage = data["usage"]
                    row["tokens_in"] += usage.get("prompt_tokens") or 0
                    row["tokens_out"] += usage.get("completion_tokens") or 0
                    row["model"] = data["model"]
                elif data.get("phase") == "executed":
                    command = approved_commands.get(data["operation_id"])
                    if command is not None and data["exit_code"] == commands[command]:
                        completed_commands.add(command)
            else:
                if time.monotonic() >= deadline:
                    row.update(outcome="timeout", reason="Истёк таймаут задачи")
        if row["outcome"] == "ok":
            if local is None:
                response = client.get(f"/v0/sessions/{session_id}/files")
                response.raise_for_status()
                result_files = Collection.model_validate(response.json()).files
                row.update(check_files(directory / "check.py", result_files))
            else:
                row.update(run_checker(directory / "check.py", local))
                result_files = {}
                if row["pass"] and (directory / "hidden_tests.py").exists():
                    result_files = {
                        name: FileData(
                            content=(local / safe_path(name)).read_text(), sha256="0" * 64
                        )
                        for name in hidden_files
                    }
            if row["pass"] and (directory / "hidden_tests.py").exists():
                if not validate_code(
                    {name: result_files[name] for name in hidden_files},
                    directory / "hidden_tests.py",
                ):
                    row.update({"pass": False, "reason": "Скрытые тесты не прошли"})
            if row["pass"] and completed_commands != set(commands):
                row.update(
                    {"pass": False, "reason": "Не выполнены команды с ожидаемыми кодами выхода"}
                )
            row["outcome"] = "passed" if row["pass"] else "check_failed"
    except httpx2.HTTPStatusError as error:
        row.update({"pass": False})
        row.update(outcome="http_error", reason=f"HTTP {error.response.status_code}")
    except httpx2.HTTPError:
        row.update({"pass": False, "outcome": "transport", "reason": "HTTP-запрос прерван"})
    except (subprocess.SubprocessError, ValueError, OSError):
        row.update({"pass": False})
        row.update(outcome="runner_error", reason="Некорректный ответ API или чекера")
    finally:
        # close идемпотентен и отменяет активную задачу, даже если ответ POST потерялся.
        try:
            api.post(client, "/v0/close", "session.close", session_id=session_id)
        except (httpx2.HTTPError, ValueError):
            row.update(
                {"pass": False, "outcome": "cleanup_error", "reason": "Закрытие не подтверждено"}
            )
        finally:
            resources.close()
    row["seconds"] = round(time.monotonic() - started, 3)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--suite", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--task", action="append", choices=TASKS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mount", action="store_true", help="Локальный Docker Desktop")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    selected = args.task or (SMOKE if args.suite == "smoke" else list(TASKS))
    if not args.mount and any(
        yaml.safe_load((TASKS[i] / "task.yaml").read_text()).get("requires_mount") for i in selected
    ):
        parser.error("Selected tasks require --mount")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output or Path(f"results/baf/early/{stamp}.jsonl")
    output.parent.mkdir(parents=True, exist_ok=True)
    inputs = [
        ROOT / "runner.py",
        ROOT / "check_task.py",
        ROOT / "workspace.py",
        ROOT.parent / "docker-compose.yml",
        ROOT.parent / "tests/infra/compose.yml",
        ROOT.parent / "services/llm-proxy/testing/mock_llm.py",
        ROOT.parent / "uv.lock",
        ROOT.parent / "services/llm-proxy/testing/baf_replay.json",
        ROOT.parent / "services/harness/config.toml",
        ROOT.parent / "services/sandbox-manager/config.toml",
    ]
    for component in ("services", "client", "contracts"):
        inputs.extend((ROOT.parent / component).rglob("*.py"))
        inputs.extend((ROOT.parent / component).rglob("Dockerfile"))
    for identifier in selected:
        inputs.extend(
            path
            for path in TASKS[identifier].rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    manifest = {
        "suite": args.suite,
        "mount": args.mount,
        "repeat": args.repeat,
        "tasks": selected,
        "python": platform.python_version(),
        "platform": platform.system() + " " + platform.machine(),
        "inputs_sha256": {
            path.relative_to(ROOT.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in inputs
        },
    }
    manifest_path = output.with_suffix(".manifest.json")
    with manifest_path.open("x", encoding="utf-8") as evidence:
        json.dump(manifest, evidence, ensure_ascii=False, indent=2)
        evidence.write("\n")
    passed = True
    with (
        output.open("x", encoding="utf-8") as results,
        api.connect(args.url, os.getenv("CLIENT_TOKEN", "local-dev-token")) as client,
    ):
        for identifier, repeat in ((i, r) for i in selected for r in range(1, args.repeat + 1)):
            row = run_task(client, TASKS[identifier], mount=args.mount)
            row["repeat"] = repeat
            results.write(json.dumps(row, ensure_ascii=False) + "\n")
            results.flush()
            print(f"{identifier}: {row['outcome']} ({row['reason']})", flush=True)
            passed &= row["pass"]
    print(f"Результаты в {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
