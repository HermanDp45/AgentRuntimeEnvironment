"""Проверка перезапуска локального Compose; в конце менеджер остановлен."""

import json
import os
import subprocess
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import docker


def harness_crash(compose, base, gateway):
    """После SIGKILL цикл утрачен; таймаут менеджера должен остановить команду."""
    token = {"Authorization": "Bearer fixture-client-token"}

    def request(url, body=None, headers=None):
        req = Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urlopen(req, timeout=10) as response:
            return json.load(response)

    def message(kind, **fields):
        return {"version": "0", "type": kind, **fields}

    def finished(operation, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = request(base + f"/v0/operations/{operation}")
            if state["state"] == "finished":
                return state["result"]
            time.sleep(0.2)
        raise AssertionError("Manager did not stop orphan command within its deadline")

    config = Path(__file__).resolve().parents[2] / "services/harness/config.toml"
    timeout = tomllib.loads(config.read_text())["tool_timeout_s"]
    assert 0 < timeout <= 120, "Crash test requires a bounded configured tool timeout"
    session, run = str(uuid4()), str(uuid4())
    fields = {"session_id": session, "run_id": run}
    try:
        request(gateway + "/v0/sessions", message("session.fixture", session_id=session), token)
        request(gateway + "/v0/tasks", message("task", **fields, message="test:cancel"), token)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = request(gateway + f"/v0/runs/{run}", headers=token)
            if state["state"] == "waiting_approval":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Harness never requested approval")
        operation = state["pending_operation_id"]
        request(
            gateway + "/v0/approvals",
            message("approval", **fields, operation_id=operation, approved=True),
            token,
        )
        stream_request = Request(gateway + f"/v0/runs/{run}/events", headers=token)
        with urlopen(stream_request, timeout=15) as stream:
            for line in stream:
                if not line.startswith(b"data: "):
                    continue
                data = json.loads(line[6:]).get("data", {})
                if data.get("phase") == "stdout" and "ready" in data.get("text", ""):
                    assert data["operation_id"] == operation
                    break
            else:
                raise AssertionError("Command did not print ready before harness crash")
        compose("kill", "-s", "SIGKILL", "harness")
        started = time.monotonic()
        result = finished(operation, timeout + 15)
        assert result["status"] == "error" and result["error"]["kind"] == "timeout", result
        elapsed = round(time.monotonic() - started, 3)
        probe = str(uuid4())
        request(
            base + "/v0/exec",
            message(
                "exec",
                session_id=session,
                operation_id=probe,
                timeout_s=5,
                operation={
                    "kind": "RunCommand",
                    "command": "python -c 'import pathlib; "
                    'print(len(list(pathlib.Path("/proc").glob("[0-9]*"))))'
                    "'",
                },
            ),
        )
        inspected = finished(probe, 10)
        assert inspected["status"] == "ok", inspected
        # Supervisor, worker, shell и проверяющий Python; спящего потомка нет.
        assert int(inspected["value"]["stdout"]) <= 4, inspected
        files = request(base + "/v0/collect", message("collect", session_id=session))["files"]
        assert files["saved.txt"]["content"] == "saved"
        compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "60", "harness")
        try:
            request(gateway + f"/v0/runs/{run}", headers=token)
        except HTTPError as error:
            assert error.code == 404, error.code
        else:
            raise AssertionError("Old run unexpectedly survived harness SIGKILL")
        return {
            "started_before_kill": True,
            "configured_timeout_s": timeout,
            "stopped_after_crash_s": elapsed,
            "descendants_cleaned": True,
            "completed_write_preserved": True,
            "old_run_lost_after_restart": True,
        }
    finally:
        request(base + "/v0/release", message("release", session_id=session))


def main():
    project = json.loads(
        subprocess.check_output(["docker", "compose", "config", "--format", "json"])
    )
    namespace = project["name"]
    address = (
        subprocess.check_output(["docker", "compose", "port", "sandbox-manager", "8081"])
        .decode()
        .strip()
    )
    base = os.environ.get("SANDBOX_TEST_URL", "http://" + address)
    compose_env = {**os.environ, "SANDBOX_MANAGER_PORT": address.rsplit(":", 1)[1]}
    client = docker.from_env()
    labels = {"label": f"are.owner={namespace}"}

    def compose(*args):
        subprocess.run(["docker", "compose", *args], check=True, env=compose_env)

    def provision():
        body = {
            "version": "0",
            "type": "provision",
            "session_id": str(uuid4()),
            "files": {"synthetic.txt": "fixture"},
        }
        request = Request(
            base + "/v0/provision",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            assert response.status == 200
        containers = client.containers.list(all=True, filters=labels)
        assert len(containers) == 1, "Run after test-infra, with no other active sessions"
        return containers[0]

    gateway_address = (
        subprocess.check_output(["docker", "compose", "port", "gateway", "8080"]).decode().strip()
    )
    crash_report = harness_crash(compose, base, "http://" + gateway_address)
    assert not client.containers.list(all=True, filters=labels)
    container = provision()
    settings = container.attrs["HostConfig"]
    assert settings["Memory"] == settings["MemorySwap"] == 1073741824
    assert settings["NanoCpus"] == 1000000000 and settings["PidsLimit"] == 128
    assert settings["NetworkMode"] == "none" and settings["ReadonlyRootfs"]
    assert settings["CapDrop"] == ["ALL"]
    assert "seccomp=builtin" in settings["SecurityOpt"]
    assert container.attrs["Config"]["User"] == "10001:10001"
    assert all(mount["Type"] == "tmpfs" for mount in container.attrs["Mounts"])
    report = {
        "harness_crash": crash_report,
        "resources": {
            key: settings[key]
            for key in (
                "Memory",
                "MemorySwap",
                "NanoCpus",
                "PidsLimit",
                "NetworkMode",
                "ReadonlyRootfs",
                "CapDrop",
                "SecurityOpt",
                "Tmpfs",
            )
        },
        "image": container.attrs["Image"],
    }
    compose("kill", "-s", "SIGKILL", "sandbox-manager")
    assert client.containers.list(all=True, filters=labels)
    compose("up", "-d", "--wait", "--wait-timeout", "60", "sandbox-manager")
    assert not client.containers.list(all=True, filters=labels)
    report["startup_orphan_cleanup"] = True
    provision()
    compose("stop", "sandbox-manager")
    assert not client.containers.list(all=True, filters=labels)
    report["graceful_cleanup"] = True
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
