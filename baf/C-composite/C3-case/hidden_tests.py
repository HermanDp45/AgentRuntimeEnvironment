from unique import unique


def test_order():
    assert unique([3, 1, 3, 2]) == [3, 1, 2]
    assert unique([]) == []


def test_candidate_tests_reject_broken_implementation(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    (tmp_path / "test_unique.py").write_text(Path("test_unique.py").read_text())
    (tmp_path / "unique.py").write_text("def unique(items): return []\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_unique.py"],
        cwd=tmp_path,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 1
