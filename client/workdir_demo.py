"""Экспорт одноразовой учебной папки WebDAV с необязательным SSH-туннелем."""

import argparse
import base64
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

RCLONE_VERSION = "rclone v1.75.2"
USER = "workdir"


def port(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Порт должен быть от 1 до 65535")
    return number


def rclone_env():
    return {
        key: value for key, value in os.environ.items() if not key.upper().startswith("RCLONE_")
    }


def ssh_command(target, ssh_port, local_port, remote_port):
    return [
        "ssh",
        "-N",
        "-T",
        "-p",
        str(ssh_port),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ControlPath=none",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-R",
        f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}",
        "--",
        target,
    ]


@contextmanager
def child(command, **options):
    process = subprocess.Popen(command, **options)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@contextmanager
def export_fixture(folder, password, local_port, read_only=False):
    env = rclone_env()
    version = subprocess.check_output(["rclone", "version"], text=True, env=env).splitlines()[0]
    if version != RCLONE_VERSION:
        raise RuntimeError(f"Для эксперимента нужен {RCLONE_VERSION}")
    env.update(RCLONE_USER=USER, RCLONE_PASS=password)
    command = [
        "rclone",
        "serve",
        "webdav",
        str(folder),
        "--config",
        os.devnull,
        "--addr",
        f"127.0.0.1:{local_port}",
        "--dir-cache-time",
        "0s",
        "--vfs-cache-mode",
        "off",
    ]
    if read_only:
        command.append("--read-only")
    token = base64.b64encode(f"{USER}:{password}".encode()).decode()
    request = Request(
        f"http://127.0.0.1:{local_port}/note.txt",
        headers={"Authorization": f"Basic {token}"},
    )
    opener = build_opener(ProxyHandler({}))
    with child(command, env=env) as process:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with opener.open(request, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                time.sleep(0.1)
        else:
            raise RuntimeError("WebDAV не запустился на локальном порту")
        yield process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-port", type=port, default=18765)
    parser.add_argument("--ssh", metavar="USER@HOST")
    parser.add_argument("--ssh-port", type=port, default=22)
    parser.add_argument("--remote-port", type=port, default=18766)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    password = getpass("Временный пароль WebDAV: ")
    if not password or any(char in password for char in "\r\n\0"):
        parser.error("Нужен непустой пароль без перевода строки")
    try:
        with tempfile.TemporaryDirectory(prefix="are-workdir-") as temporary:
            folder = Path(temporary)
            (folder / "note.txt").write_bytes(b"client-original\n")
            with export_fixture(folder, password, args.local_port, args.read_only) as exporter:
                print(f"Учебная папка: {folder}", flush=True)
                if args.ssh:
                    command = ssh_command(
                        args.ssh, args.ssh_port, args.local_port, args.remote_port
                    )
                    with child(command) as ssh:
                        while exporter.poll() is None and ssh.poll() is None:
                            time.sleep(0.25)
                        if ssh.returncode is not None:
                            return ssh.returncode
                        raise RuntimeError("Локальный экспорт завершился, туннель закрыт")
                print(f"WebDAV слушает 127.0.0.1:{args.local_port}. Остановка: Ctrl+C.", flush=True)
                return exporter.wait()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Ошибка запуска: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
