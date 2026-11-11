"""Настоящие HTTP-запросы к Compose; используются только синтетические файлы."""

import json
import os
import shlex
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

BASE = os.environ.get("SANDBOX_TEST_URL", "").rstrip("/")
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not BASE, reason="Set SANDBOX_TEST_URL for Compose"),
]


def http(path, body=None, headers=None):
    request = Request(
        BASE + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urlopen(request, timeout=20) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if not path.endswith("events") else text
    except HTTPError as error:
        return error.code, json.load(error)


def message(kind, **data):
    return {"version": "0", "type": kind, **data}


@pytest.fixture
def sessions():
    created = []

    def create(files=None, read_only=False):
        identifier = str(uuid4())
        created.append(identifier)
        body = message("provision", session_id=identifier, files=files or {}, read_only=read_only)
        assert http("/v0/provision", body)[0] == 200
        assert http("/v0/provision", body)[0] == 200
        return identifier

    yield create
    for identifier in created:
        assert http("/v0/release", message("release", session_id=identifier))[0] == 200


def start(session, operation, timeout_s=10):
    body = message(
        "exec",
        session_id=session,
        operation_id=str(uuid4()),
        operation=operation,
        timeout_s=timeout_s,
    )
    status, _ = http("/v0/exec", body)
    assert status == 202
    return body


def finish(body):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, state = http(f"/v0/operations/{body['operation_id']}")
        assert status == 200
        if state["state"] == "finished":
            # Финальное событие может опередить освобождение session.busy на несколько мс.
            time.sleep(0.03)
            return state["result"]
        time.sleep(0.03)
    pytest.fail("Operation did not finish")


def command(session, command, timeout_s=10):
    return finish(start(session, {"kind": "RunCommand", "command": command}, timeout_s))


def test_files_command_errors_sse_and_idempotency(sessions):
    session = sessions({"note.txt": "original"})
    read = finish(start(session, {"kind": "ReadFile", "path": "note.txt"}))
    assert read["value"]["content"] == "original"
    write = start(
        session,
        {
            "kind": "WriteFile",
            "path": "note.txt",
            "content": "changed",
            "expected_sha256": read["value"]["sha256"],
        },
    )
    assert finish(write)["status"] == "ok"
    # Повторный WriteFile с прежним sha не исполняется заново и не даёт write_conflict.
    assert http("/v0/exec", write)[1]["result"]["status"] == "ok"
    conflict = write | {"operation_id": str(uuid4())}
    assert http("/v0/exec", conflict)[0] == 202
    assert finish(conflict)["error"]["code"] == "write_conflict"
    assert http("/v0/exec", write | {"timeout_s": 1})[0] == 409
    failed = start(session, {"kind": "RunCommand", "command": "echo out; echo err >&2; exit 7"})
    result = finish(failed)
    assert result["error"]["kind"] == "command"
    assert result["value"]["exit_code"] == 7
    assert result["value"]["stdout"] == "out\n"
    assert result["value"]["stderr"] == "err\n"
    url = f"/v0/operations/{failed['operation_id']}/events"
    stream = http(url)[1]
    assert stream.count("event: result") == 1
    assert "event: stdout" in stream and "event: stderr" in stream
    assert "event: started" not in http(url, headers={"Last-Event-ID": "1"})[1]
    unicode_op = start(session, {"kind": "RunCommand", "command": "python -c 'print(\"я\"*5000)'"})
    unicode_result = finish(unicode_op)
    unicode_stream = http(f"/v0/operations/{unicode_op['operation_id']}/events")[1]
    stdout = []
    for line in unicode_stream.splitlines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            if event["type"] == "stdout":
                stdout.append(event["text"])
    assert "".join(stdout) == unicode_result["value"]["stdout"] == "я" * 5000 + "\n"
    collected = http("/v0/collect", message("collect", session_id=session))[1]
    assert collected["files"]["note.txt"]["content"] == "changed"
    assert http("/v0/release", message("release", session_id=session))[0] == 200
    assert http("/v0/release", message("release", session_id=session))[0] == 200


def test_cancel_kills_detached_descendants_and_preserves_finished_files(sessions):
    session = sessions({"keep.txt": "keep"})
    body = start(
        session,
        {
            "kind": "RunCommand",
            "command": (
                "printf saved > keep.txt; python -c 'import os,time; os.setsid(); "
                'print("ready",flush=True); time.sleep(300)\' & wait'
            ),
        },
        timeout_s=15,
    )
    # Дождаться запуска дочернего процесса, а не просто принятия HTTP-запроса.
    with urlopen(BASE + f"/v0/operations/{body['operation_id']}/events", timeout=5) as stream:
        while line := stream.readline():
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                if event["type"] == "stdout" and "ready" in event["text"]:
                    break
        else:
            pytest.fail("Detached child never started")
    status, state = http("/v0/cancel", message("cancel", operation_id=body["operation_id"]))
    assert status == 200 and state["result"]["status"] == "cancelled"
    assert http("/v0/cancel", message("cancel", operation_id=body["operation_id"]))[0] == 200
    check = command(
        session,
        'python -c \'import pathlib; print(len(list(pathlib.Path("/proc").glob("[0-9]*"))))\'',
    )
    # PID1 + worker + shell + проверяющий Python. Спящий потомок сюда не входит.
    assert int(check["value"]["stdout"]) <= 4
    assert (
        finish(start(session, {"kind": "ReadFile", "path": "keep.txt"}))["value"]["content"]
        == "saved"
    )
    timed = command(session, "sleep 300", timeout_s=0.15)
    assert timed["error"]["kind"] == "timeout"
    assert command(session, "echo reusable")["status"] == "ok"


def test_isolation_read_only_limits_and_symlinks(sessions):
    session = sessions({"a.txt": "private"})
    other = sessions({"a.txt": "other"}, read_only=True)
    denied = command(other, "echo bad > a.txt")
    assert denied["status"] == "error"
    assert (
        finish(start(other, {"kind": "ReadFile", "path": "a.txt"}))["value"]["content"] == "other"
    )
    inspected = command(
        session,
        "python -c 'import os,pathlib; print(os.getuid()); "
        'print(pathlib.Path("/proc/self/status").read_text()); '
        'print(pathlib.Path("/var/run/docker.sock").exists()); '
        'print("API_KEY" in os.environ, "LLM_API_KEY" in os.environ)\' ',
    )
    text = inspected["value"]["stdout"]
    assert text.startswith("10001\n")
    assert "CapEff:\t0000000000000000" in text
    assert "NoNewPrivs:\t1" in text and "Seccomp:\t2" in text
    assert text.endswith("False\nFalse False\n")
    assert command(session, "touch /root-write")["status"] == "error"
    network = command(
        session, "python -c 'import socket; socket.create_connection((\"1.1.1.1\",443),timeout=1)' "
    )
    assert network["status"] == "error"
    assert command(session, "ln -s /etc/passwd link")["status"] == "ok"
    assert finish(start(session, {"kind": "ReadFile", "path": "link"}))["status"] == "error"
    full = command(session, 'python -c \'open("full","wb").write(b"x"*(65*1024*1024))\'')
    assert full["status"] == "error" and "No space left" in full["value"]["stderr"]
    output = command(other, "python -c 'print(\"x\"*100000)' ")
    assert output["value"]["truncated"] is True
    assert len(output["value"]["stdout"].encode()) <= 65536


def test_parallel_sessions_and_release_running_command(sessions):
    first, second = sessions(), sessions()
    busy = start(first, {"kind": "RunCommand", "command": "sleep 300"})
    assert command(second, "echo independent")["value"]["stdout"] == "independent\n"
    assert http("/v0/release", message("release", session_id=first))[0] == 200
    assert http(f"/v0/operations/{busy['operation_id']}")[0] == 404
    assert command(second, "echo still-alive")["status"] == "ok"


def python_probe(session, source, timeout_s=15):
    result = command(session, "python -c " + shlex.quote(source), timeout_s=timeout_s)
    assert result["status"] == "ok", result
    return json.loads(result["value"]["stdout"])


def test_cpu_quota_throttles_bounded_parallel_load(sessions):
    session = sessions()
    observed = python_probe(
        session,
        r"""
import json, os, pathlib, subprocess, sys
root = pathlib.Path('/sys/fs/cgroup')
quota, period = root.joinpath('cpu.max').read_text().split()
assert quota != 'max' and 0 < int(quota) <= int(period)
assert len(os.sched_getaffinity(0)) >= 2, 'CPU test needs two available CPUs'
def throttled():
    fields = dict(line.split() for line in root.joinpath('cpu.stat').read_text().splitlines())
    return int(fields['nr_throttled'])
before = throttled()
children = []
try:
    for _ in range(4):
        children.append(subprocess.Popen([sys.executable, '-c',
            'import time; end=time.monotonic()+2;\nwhile time.monotonic()<end: pass']))
    for child in children:
        assert child.wait(timeout=5) == 0
finally:
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait()
print(json.dumps({'before': before, 'after': throttled()}))
""",
    )
    assert observed["after"] > observed["before"], observed
    assert command(session, "echo reusable")["status"] == "ok"


def test_memory_limit_kills_allocating_child_and_session_survives(sessions):
    session = sessions({"keep.txt": "keep"})
    observed = python_probe(
        session,
        """
import json, pathlib, subprocess, sys
root = pathlib.Path('/sys/fs/cgroup')
limit = int(root.joinpath('memory.max').read_text())
assert 0 < limit <= 1024**3, 'Refuse unbounded memory probe'
assert root.joinpath('memory.swap.max').read_text().strip() == '0'
def killed():
    lines = root.joinpath('memory.events').read_text().splitlines()
    return int(dict(line.split() for line in lines)['oom_kill'])
before = killed()
# Максимальный oom_score_adj делает нагрузочный процесс первым кандидатом OOM.
source = ("from pathlib import Path; "
          "Path('/proc/self/oom_score_adj').write_text('1000'); "
          "data=bytearray(%d)" % (limit + 8*1024**2))
child = subprocess.Popen([sys.executable, '-c', source])
try:
    code = child.wait(timeout=10)
finally:
    if child.poll() is None:
        child.kill()
    child.wait()
print(json.dumps({'code': code, 'before': before, 'after': killed()}))
""",
    )
    assert observed["code"] == -9, observed
    assert observed["after"] > observed["before"], observed
    assert (
        finish(start(session, {"kind": "ReadFile", "path": "keep.txt"}))["value"]["content"]
        == "keep"
    )


def test_pid_limit_rejects_bounded_spawn_and_recovers(sessions):
    session = sessions()
    observed = python_probe(
        session,
        """
import errno, json, os, pathlib, signal, time
root = pathlib.Path('/sys/fs/cgroup')
limit = int(root.joinpath('pids.max').read_text())
assert 0 < limit <= 128, 'Refuse unbounded process probe'
children, blocked = [], False
try:
    for _ in range(limit + 1):
        try:
            pid = os.fork()
            if pid == 0:
                time.sleep(8)
                os._exit(0)
            children.append(pid)
        except OSError as error:
            assert error.errno == errno.EAGAIN
            blocked = True
            break
    peak = int(root.joinpath('pids.current').read_text())
finally:
    for child in children:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in children:
        os.waitpid(child, 0)
print(json.dumps({'blocked': blocked, 'spawned': len(children), 'peak': peak, 'limit': limit}))
""",
    )
    assert observed["blocked"] and 0 < observed["spawned"] < observed["limit"], observed
    assert observed["peak"] <= observed["limit"], observed
    assert command(session, "echo reusable")["value"]["stdout"] == "reusable\n"


@pytest.mark.skipif(not os.environ.get("HARNESS_TEST_URL"), reason="Canary needs offline Compose")
def test_server_canaries_are_absent_from_sandbox(sessions):
    session = sessions()
    observed = python_probe(
        session,
        """
import json, os, pathlib
values = [str(dict(os.environ))]
for path in pathlib.Path('/proc').glob('[0-9]*/environ'):
    try:
        values.append(path.read_bytes().decode(errors='replace'))
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        pass
print(json.dumps({'values': values, 'secrets': pathlib.Path('/run/secrets').exists()}))
""",
    )
    # Значения остаются в тестовом процессе и не передаются команде сандбокса.
    assert "fixture-provider-secret" not in str(observed)
    assert "fixture-manager-canary-C2" not in str(observed)
    assert not observed["secrets"]
