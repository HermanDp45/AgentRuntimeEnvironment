"""Проверка реального обратного SSH-туннеля и Linux FUSE на Docker Desktop."""

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from client.workdir_demo import child, export_fixture, ssh_command
from tests.infra.webdav_probe import IMAGE, SCRIPT, checked, docker

SSH_IMAGE = (
    "gcr.io/k8s-minikube/kicbase:v0.0.51"
    "@sha256:4a1c825b61479e6c898851ea66f13c620aaeab6002746e95067fc2c4b38a0b24"
)
SSH_SETUP = """mkdir -p /run/sshd
ssh-keygen -q -t ed25519 -N '' -f /tmp/host
printf '%s\n' "$ARE_TEST_KEY" >/tmp/authorized
unset ARE_TEST_KEY
exec /usr/sbin/sshd -D -e -f /dev/null -h /tmp/host -p 2222 \
    -o AuthorizedKeysFile=/tmp/authorized -o StrictModes=no -o UsePAM=yes \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o PermitRootLogin=prohibit-password -o AllowTcpForwarding=remote \
    -o GatewayPorts=no -o PermitListen=127.0.0.1:18766 -o ForceCommand=/bin/false
"""


def probe():
    suffix = uuid4().hex[:12]
    ssh_name, mount_name = f"are-webdav-ssh-{suffix}", f"are-webdav-fuse-{suffix}"
    password = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        client_port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="are-webdav-ssh-") as tmp:
        root = Path(tmp)
        folder = root / "fixture"
        folder.mkdir()
        note = folder / "note.txt"
        note.write_bytes(b"through-ssh\n")
        key = root / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        try:
            checked(
                docker(
                    "run",
                    "--rm",
                    "--detach",
                    "--pull",
                    "never",
                    "--name",
                    ssh_name,
                    "--label",
                    "are.experiment=webdav",
                    "-p",
                    "127.0.0.1::2222",
                    "-e",
                    "ARE_TEST_KEY",
                    "--entrypoint",
                    "sh",
                    SSH_IMAGE,
                    "-ec",
                    SSH_SETUP,
                    env=os.environ | {"ARE_TEST_KEY": key.with_suffix(".pub").read_text()},
                )
            )
            ssh_port = int(checked(docker("port", ssh_name, "2222/tcp")).rsplit(":", 1)[1])
            host_key = checked(
                docker(
                    "exec",
                    ssh_name,
                    "sh",
                    "-ec",
                    "until test -s /tmp/host.pub; do sleep 0.1; done; cat /tmp/host.pub",
                )
            )
            known = root / "known_hosts"
            known.write_text(f"[127.0.0.1]:{ssh_port} {host_key}\n")
            command = ssh_command("root@127.0.0.1", ssh_port, client_port, 18766)
            command[1:1] = [
                "-F",
                "/dev/null",
                "-i",
                str(key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                f"UserKnownHostsFile={known}",
            ]
            with export_fixture(folder, password, client_port), child(command) as ssh:
                checked(
                    docker(
                        "run",
                        "--rm",
                        "--detach",
                        "--pull",
                        "never",
                        "--name",
                        mount_name,
                        "--label",
                        "are.experiment=webdav",
                        "--network",
                        f"container:{ssh_name}",
                        "--cap-drop",
                        "ALL",
                        "--cap-add",
                        "SYS_ADMIN",
                        "--device",
                        "/dev/fuse",
                        "--mount",
                        f"type=bind,source={SCRIPT},target=/mount-workdir.sh,readonly",
                        "-e",
                        "ARE_WEBDAV_PASSWORD",
                        "--entrypoint",
                        "sh",
                        IMAGE,
                        "-ec",
                        "mkdir /work; exec sh /mount-workdir.sh /work",
                        env=os.environ | {"ARE_WEBDAV_PASSWORD": password},
                    )
                )
                for _ in range(25):
                    reply = docker(
                        "exec", "--user", "10001:10001", mount_name, "cat", "/work/note.txt"
                    )
                    if reply.returncode == 0:
                        break
                    if ssh.poll() is not None:
                        raise RuntimeError("SSH forwarding failed")
                    time.sleep(0.2)
                assert checked(reply) == "through-ssh"
                checked(
                    docker(
                        "exec",
                        "--user",
                        "10001:10001",
                        mount_name,
                        "sh",
                        "-ec",
                        "printf 'ssh-reply\\n' >/work/reply",
                    )
                )
                assert (folder / "reply").read_bytes() == b"ssh-reply\n"
        except Exception:
            for name in (mount_name, ssh_name):
                print(docker("logs", name).stderr, flush=True)
            raise
        finally:
            for name in (mount_name, ssh_name):
                if docker("inspect", name).returncode == 0:
                    checked(docker("rm", "-f", name))
        return {
            "ssh_image": SSH_IMAGE,
            "mount_image": IMAGE,
            "loopback_reverse_ssh": True,
            "client_to_fuse": True,
            "fuse_to_client": True,
            "containers_removed": True,
        }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))
