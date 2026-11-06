"""Проверка перезапуска локального Compose; в конце менеджер остановлен."""

import json
import os
import subprocess
from urllib.request import Request, urlopen
from uuid import uuid4

import docker


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
