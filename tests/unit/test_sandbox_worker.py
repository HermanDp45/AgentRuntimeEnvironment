import os
from pathlib import Path

import pytest
import sandbox_worker as worker


def write(root, content="new", expected="missing"):
    return worker.write_file(root, "a.txt", content, expected, "._are_test.tmp", 65536)


def test_new_file_and_checked_replacement(tmp_path):
    first = write(tmp_path)
    (tmp_path / "a.txt").chmod(0o640)
    second = write(tmp_path, "next", first["sha256"])
    assert worker.read_file(tmp_path, "a.txt", 65536) == {
        "content": "next",
        "sha256": second["sha256"],
    }
    assert (tmp_path / "a.txt").stat().st_mode & 0o777 == 0o640
    with pytest.raises(worker.WorkerError) as error:
        write(tmp_path, "stale", first["sha256"])
    assert error.value.code == "write_conflict"
    assert (tmp_path / "a.txt").read_text() == "next"


@pytest.mark.parametrize("existing", [True, False])
def test_interrupted_publish_never_exposes_partial_file(tmp_path, monkeypatch, existing):
    expected = write(tmp_path, "original")["sha256"] if existing else "missing"

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace" if existing else "link", interrupt)
    with pytest.raises(KeyboardInterrupt):
        write(tmp_path, "replacement", expected)
    assert not list(tmp_path.glob("._are_*"))
    assert (
        (tmp_path / "a.txt").read_text() == "original"
        if existing
        else not (tmp_path / "a.txt").exists()
    )


def test_no_read_or_write_through_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("private")
    root = tmp_path / "work"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "a.txt").symlink_to(outside / "a.txt")
    for path in ("link/a.txt", "a.txt"):
        with pytest.raises(OSError):
            worker.read_file(root, path, 65536)
        with pytest.raises(OSError):
            worker.write_file(root, path, "bad", "missing", "._are_test.tmp", 65536)
    assert (outside / "a.txt").read_text() == "private"


def test_fifo_read_fails_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(worker.WorkerError) as error:
        worker.read_file(tmp_path, "pipe", 65536)
    assert error.value.code == "invalid_path"


def test_kill_refuses_to_run_on_host(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda _: False)
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("Must never send a host signal"))
    with pytest.raises(worker.WorkerError) as error:
        worker.kill_commands()
    assert error.value.code == "unsafe_environment"


def test_kill_is_successful_when_no_commands_remain(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda _: True)
    monkeypatch.setattr(Path, "read_bytes", lambda _: b"python\0/opt/sandbox_worker.py\0idle\0")

    def no_processes(pid, sig):
        assert pid == -1
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", no_processes)
    assert worker.kill_commands() == {}
