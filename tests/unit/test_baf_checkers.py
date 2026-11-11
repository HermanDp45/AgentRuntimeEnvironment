"""Чекеры должны отклонять неверный результат и порчу исходных данных."""

import json
import shutil

import pytest

from baf.runner import TASKS, run_checker


@pytest.mark.parametrize("identifier", ["B9.1", "B9.2", "B9.3"])
def test_normalization_checker_rejects_wrong_result_and_source_change(tmp_path, identifier):
    task = TASKS[identifier]
    shutil.copytree(task / "fixture", tmp_path, dirs_exist_ok=True)
    expected = json.loads((task / "expected.json").read_text())
    output = tmp_path / "result.json"
    output.write_text(json.dumps(expected))
    assert run_checker(task / "check.py", tmp_path)["pass"]
    output.write_text('{"rows": [], "errors": []}')
    assert not run_checker(task / "check.py", tmp_path)["pass"]
    output.write_text(json.dumps(expected))
    (tmp_path / "input.csv").write_text("corrupted")
    assert not run_checker(task / "check.py", tmp_path)["pass"]


@pytest.mark.parametrize("identifier", sorted(TASKS))
def test_corpus_checkers_accept_reference_and_reject_missing_output(tmp_path, identifier):
    from pathlib import Path

    import yaml

    task = TASKS[identifier]
    spec = yaml.safe_load((task / "task.yaml").read_text())
    replay = json.loads(Path("services/llm-proxy/testing/baf_replay.json").read_text())
    steps = replay["\n".join(spec["messages"])]
    shutil.copytree(task / "fixture", tmp_path, dirs_exist_ok=True)
    for step in steps:
        if step["name"] == "write_file":
            args = step["arguments"]
            path = tmp_path / args["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(args["content"].encode(args.get("encoding", "utf-8")))
    assert run_checker(task / "check.py", tmp_path)["pass"]
    (tmp_path / spec["answer_file"]).unlink()
    assert not run_checker(task / "check.py", tmp_path)["pass"]
