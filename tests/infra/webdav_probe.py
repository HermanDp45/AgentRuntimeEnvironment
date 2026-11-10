"""Mac + Linux Docker Desktop: экспорт, FUSE, права и обрыв на синтетической папке."""

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from client.workdir_demo import export_fixture

IMAGE = (
    "rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5"
)
SCRIPT = Path(__file__).resolve().parents[2] / "services/sandbox-manager/mount-workdir.sh"
# В Linux VM локальный порт имитирует выход туннеля через host.docker.internal.
START = """mkdir /work
printf '#!/bin/sh\nexec nc host.docker.internal "$ARE_TEST_PORT"\n' >/tmp/relay
chmod 700 /tmp/relay
nc -lk -s 127.0.0.1 -p 18766 -e /tmp/relay &
exec sh /mount-workdir.sh /work 18766 "$ARE_TEST_MODE"
"""


def docker(*args, env=None, timeout=20):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, env=env, timeout=timeout, check=False
    )


def checked(result):
    result.check_returncode()
    return result.stdout.strip()


def probe(mode):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = secrets.token_urlsafe(24)
    name = f"are-webdav-{uuid4().hex[:12]}"
    environment = os.environ | {
        "ARE_WEBDAV_PASSWORD": password,
        "ARE_TEST_PORT": str(port),
        "ARE_TEST_MODE": mode,
    }

    def work(command):
        return docker("exec", "--user", "10001:10001", name, "sh", "-ec", command, timeout=12)

    def wait_work(command):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            reply = work(command)
            if reply.returncode == 0:
                return reply.stdout.strip()
            time.sleep(0.2)
        raise RuntimeError(f"Linux probe did not finish: {command}")

    with tempfile.TemporaryDirectory(prefix="are-webdav-test-") as directory:
        folder = Path(directory)
        note = folder / "note.txt"
        note.write_text("client-original\n", encoding="utf-8")
        result = {"mode": mode}
        try:
            with export_fixture(folder, password, port, mode == "ro"):
                checked(
                    docker(
                        "run",
                        "--detach",
                        "--rm",
                        "--pull",
                        "never",
                        "--name",
                        name,
                        "--label",
                        "are.experiment=webdav",
                        "--cap-drop",
                        "ALL",
                        "--cap-add",
                        "SYS_ADMIN",
                        "--add-host",
                        "host.docker.internal:host-gateway",
                        "--device",
                        "/dev/fuse",
                        "--mount",
                        f"type=bind,source={SCRIPT},target=/mount-workdir.sh,readonly",
                        "-e",
                        "ARE_WEBDAV_PASSWORD",
                        "-e",
                        "ARE_TEST_PORT",
                        "-e",
                        "ARE_TEST_MODE",
                        "--entrypoint",
                        "sh",
                        IMAGE,
                        "-ec",
                        START,
                        env=environment,
                    )
                )
                assert wait_work("mountpoint -q /work && cat /work/note.txt") == "client-original"
                result["initial_read"] = True
                note.write_text("client-edited\n", encoding="utf-8")
                assert checked(work("cat /work/note.txt")) == "client-edited"
                result["client_to_linux"] = True
                if mode == "rw":
                    checked(
                        work(
                            "printf 'linux-edited\\n' >/work/staged && "
                            "mv /work/staged /work/note.txt"
                        )
                    )
                    assert note.read_text(encoding="utf-8") == "linux-edited\n"
                    assert not (folder / "staged").exists()
                    result["linux_to_client"] = True
                    result["rename_over_existing"] = True
                    checked(
                        docker(
                            "exec",
                            "-d",
                            "--user",
                            "10001:10001",
                            name,
                            "sh",
                            "-c",
                            "(set -e; exec 3>/work/inflight; printf part >&3; touch /tmp/started; "
                            "while [ ! -e /tmp/resume ]; do sleep 0.1; done; printf end >&3; "
                            "exec 3>&-; mv /work/inflight /work/note.txt); "
                            "echo $? >/tmp/write-status",
                        )
                    )
                    wait_work("test -f /tmp/started")
                else:
                    refused = work("printf 'unexpected\\n' >/work/note.txt")
                    assert refused.returncode != 0
                    assert note.read_text(encoding="utf-8") == "client-edited\n"
                    result["write_refused"] = True
            started = time.monotonic()
            interrupted = work("cat /work/note.txt")
            assert interrupted.returncode != 0
            result["read_fails_after_disconnect"] = True
            result["disconnect_read_seconds"] = round(time.monotonic() - started, 3)
            if mode == "rw":
                checked(work("touch /tmp/resume"))
                assert int(wait_work("test -s /tmp/write-status && cat /tmp/write-status")) != 0
                assert note.read_text(encoding="utf-8") == "linux-edited\n"
                result["original_preserved_during_disconnect"] = True
                rejected = work("printf 'offline\\n' >/work/disconnected")
                assert rejected.returncode != 0
                assert not (folder / "disconnected").exists()
                result["write_fails_after_disconnect"] = True
            with export_fixture(folder, password, port, mode == "ro"):
                expected = "linux-edited" if mode == "rw" else "client-edited"
                assert wait_work("cat /work/note.txt") == expected
                result["read_recovers_after_restart"] = True
        except Exception:
            logs = docker("logs", name)
            print(logs.stdout + logs.stderr, flush=True)
            raise
        finally:
            if docker("inspect", name).returncode == 0:
                checked(docker("rm", "-f", name))
        assert docker("inspect", name).returncode != 0
        result["container_removed"] = True
        return result


if __name__ == "__main__":
    print(json.dumps([probe("rw"), probe("ro")], indent=2))
