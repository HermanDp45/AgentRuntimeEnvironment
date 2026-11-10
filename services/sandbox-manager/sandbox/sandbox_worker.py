"""Доверенные файловые операции и запуск команд внутри контейнера."""

import codecs
import errno
import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from contracts.paths import safe_path


class WorkerError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def emit(event):
    print(json.dumps(event, ensure_ascii=True), flush=True)


@contextmanager
def parent(root, path, create=False):
    parts = safe_path(path).split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def read_bytes(fd, name, limit):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise WorkerError("invalid_path", "Only regular files are supported")
        data = file.read(limit + 1)
        if len(data) > limit:
            raise WorkerError("file_too_large", "File exceeds the configured limit")
        return data, stat.S_IMODE(info.st_mode)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_file(root, path, limit):
    with parent(root, path) as (fd, name):
        data, _ = read_bytes(fd, name, limit)
    return {"content": data.decode("utf-8"), "sha256": digest(data)}


def current(fd, name, limit):
    try:
        data, mode = read_bytes(fd, name, limit)
        return digest(data), mode
    except FileNotFoundError:
        return "missing", 0o600


def write_file(root, path, content, expected, stage, limit):
    data = content.encode("utf-8")
    if len(data) > limit:
        raise WorkerError("file_too_large", "File exceeds the configured limit")
    if not stage.startswith("._are_") or "/" in stage:
        raise WorkerError("invalid_stage", "Invalid temporary file name")
    with parent(root, path, create=True) as (fd, name):
        before, mode = current(fd, name, limit)
        if before != expected:
            raise WorkerError("write_conflict", "File changed since it was reviewed")
        handle = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=fd)
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
                os.fchmod(file.fileno(), mode)
            if current(fd, name, limit)[0] != expected:
                raise WorkerError("write_conflict", "File changed while the write was prepared")
            if expected == "missing":
                os.link(stage, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            else:
                os.replace(stage, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(stage, dir_fd=fd)
            except FileNotFoundError:
                pass
    return {"sha256": digest(data), "bytes": len(data)}


def clean_stage(root, path, stage):
    if not stage.startswith("._are_") or "/" in stage:
        raise WorkerError("invalid_stage", "Invalid temporary file name")
    with parent(root, path) as (fd, _):
        try:
            os.unlink(stage, dir_fd=fd)
        except FileNotFoundError:
            pass
    return {}


def collect(root, limit, mounted=False):
    files, size, entries = {}, 0, 0

    def report(error):
        if mounted:
            raise error

    for directory, names, leaves in os.walk(root, followlinks=False, onerror=report):
        entries += len(names) + len(leaves)
        if entries > 512:
            if mounted:
                return files
            raise WorkerError("collection_too_large", "Too many directory entries")
        names[:] = [name for name in names if name != ".git"]
        for name in names + leaves:
            if name.startswith("._are_") or name == ".git":
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise WorkerError("invalid_path", "Symbolic links are not collected")
            if name in leaves:
                relative = path.relative_to(root).as_posix()
                try:
                    data = read_file(root, relative, limit)
                except (UnicodeError, WorkerError):
                    if mounted:
                        continue
                    raise
                if mounted and (len(files) >= 128 or size >= 256 * 1024):
                    return files
                size += len(data["content"].encode("utf-8"))
                if len(files) >= 128 or size > 128 * limit:
                    raise WorkerError("collection_too_large", "Collection exceeds its limit")
                files[relative] = data
    return files


def run_command(command, limit):
    process = subprocess.Popen(
        command,
        shell=True,
        executable="/bin/sh",
        cwd="/work",
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    outputs, remaining, truncated = {"stdout": [], "stderr": []}, limit, False
    decoders = {name: codecs.getincrementaldecoder("utf-8")(errors="replace") for name in outputs}
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                ready = selector.select(timeout=0.05)
                if not ready and process.poll() is not None:
                    break
                for key, _ in ready:
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    clipped, remaining = data[:remaining], max(0, remaining - len(data))
                    truncated |= len(clipped) < len(data)
                    if clipped:
                        outputs[key.data].append(clipped)
                        text = decoders[key.data].decode(clipped)
                        if text:
                            emit({"type": key.data, "text": text})
            process.wait()
            for name, decoder in decoders.items():
                if tail := decoder.decode(b"", final=True):
                    emit({"type": name, "text": tail})
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
    return {
        "exit_code": process.returncode,
        "truncated": truncated,
        **{
            name: b"".join(parts).decode("utf-8", errors="replace")
            for name, parts in outputs.items()
        },
    }


def kill_commands():
    pid_one = Path("/proc/1/cmdline")
    if not Path("/.dockerenv").exists() or not pid_one.exists():
        raise WorkerError("unsafe_environment", "Cancellation requires the sandbox container")
    command = pid_one.read_bytes().split(b"\0")
    if b"/opt/sandbox_worker.py" not in command or b"idle" not in command:
        raise WorkerError("unsafe_environment", "Unexpected sandbox supervisor")
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass  # ESRCH: кроме PID1 и вызывающего процесса уже никого нет.
    return {}


def execute(request, root=Path("/work")):
    kind, limit = request["kind"], request.get("limit", 65536)
    if kind == "ReadFile":
        return read_file(root, request["path"], limit)
    if kind == "WriteFile":
        return write_file(
            root,
            request["path"],
            request["content"],
            request["expected_sha256"],
            request["stage"],
            limit,
        )
    if kind == "Collect":
        return {"files": collect(root, limit, request.get("mounted", False))}
    if kind == "CleanStage":
        return clean_stage(root, request["path"], request["stage"])
    if kind == "Freeze":
        for directory, _, files in os.walk(root, topdown=False):
            for name in files:
                os.chmod(Path(directory) / name, 0o444)
            os.chmod(directory, 0o555)
        return {}
    if kind == "RunCommand":
        return run_command(request["command"], limit)
    if kind == "Kill":
        return kill_commands()
    raise WorkerError("invalid_operation", "Unknown operation")


def main():
    if sys.argv[1] == "idle":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        while True:
            time.sleep(3600)
    if sys.argv[1] == "check":
        import pytest

        print(f"worker ready; pytest {pytest.__version__}")
        return
    try:
        value = execute(json.loads("".join(sys.argv[1:])))
        if value.get("exit_code", 0) != 0:
            emit(
                {
                    "type": "result",
                    "ok": False,
                    "value": value,
                    "error": {
                        "kind": "command",
                        "code": "nonzero_exit",
                        "message": "Command failed",
                    },
                }
            )
        else:
            emit({"type": "result", "ok": True, "value": value})
    except (OSError, ValueError, WorkerError) as error:
        code = {
            errno.ENOENT: "not_found",
            errno.ELOOP: "invalid_path",
            errno.ENOTDIR: "invalid_path",
            errno.EACCES: "permission_denied",
            errno.EROFS: "read_only",
            errno.ENOSPC: "storage_full",
            errno.EEXIST: "write_conflict",
        }.get(getattr(error, "errno", None), "operation_failed")
        if isinstance(error, WorkerError):
            code = error.code
        if isinstance(error, UnicodeError):
            code = "not_utf8"
        emit(
            {
                "type": "result",
                "ok": False,
                "value": {},
                "error": {"kind": "sandbox", "code": code, "message": code.replace("_", " ")},
            }
        )


if __name__ == "__main__":
    main()
