"""Чекер данных: эталоны вне workdir, код кандидата здесь не исполняется."""

import ast
import json
from pathlib import Path

import yaml


def check(task, work):
    task, work = Path(task), Path(work)
    spec = json.loads((task / "expected.json").read_text())
    editable = set(spec["outputs"])
    try:
        for name, rule in spec["outputs"].items():
            path = work / name
            if path.is_symlink() or not path.is_file():
                return False
            data = path.read_bytes()
            if "ast" in rule:
                if ast.dump(ast.parse(data)) != ast.dump(ast.parse(rule["ast"])):
                    return False
            elif "yaml" in rule:
                if json.dumps(yaml.safe_load(data), sort_keys=True) != json.dumps(
                    rule["yaml"], sort_keys=True
                ):
                    return False
            elif "json" in rule:
                if json.dumps(json.loads(data), sort_keys=True) != json.dumps(
                    rule["json"], sort_keys=True
                ):
                    return False
            elif "text" in rule:
                text = data.decode(rule.get("encoding", "utf-8"))
                if rule.get("strip", False):
                    text = text.strip()
                if text != rule["text"]:
                    return False
        for original in (task / "fixture").rglob("*"):
            name = original.relative_to(task / "fixture").as_posix()
            if original.is_file() and name not in editable:
                if (work / name).is_symlink() or (
                    work / name
                ).read_bytes() != original.read_bytes():
                    return False
    except (OSError, UnicodeError, ValueError, SyntaxError):
        return False
    return True


def main(task, work):
    ok = check(task, work)
    print(
        json.dumps(
            {"pass": ok, "reason": "ok" if ok else "Неверный результат или изменён источник"}
        )
    )
