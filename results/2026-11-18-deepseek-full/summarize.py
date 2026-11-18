"""Собрать три полных прохода в одну таблицу и проверить покрытие."""

import json
import statistics
import sys
from collections import Counter
from pathlib import Path


def main(root):
    rows = []
    manifest = json.loads((root / "round-1.manifest.json").read_text())
    tasks = manifest["tasks"]
    for number in (1, 2, 3):
        current = json.loads((root / f"round-{number}.manifest.json").read_text())
        if current != manifest:
            raise ValueError(f"Различаются манифесты проходов 1 и {number}")
        records = [
            json.loads(line) for line in (root / f"round-{number}.jsonl").read_text().splitlines()
        ]
        if [r["task_id"] for r in records] != tasks:
            raise ValueError(f"Проход {number} неполный или изменён порядок задач")
        rows.extend(dict(record, repeat=number) for record in records)
    if len(tasks) != 39 or len({r["run_id"] for r in rows}) != 117:
        raise ValueError("Ожидаются 117 независимых запусков на 39 задачах")
    (root / "full.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    )
    stats = {}
    for group in ("all", *dict.fromkeys(t.split(".")[0] for t in tasks)):
        sample = [r for r in rows if group == "all" or r["task_id"].split(".")[0] == group]
        stats[group] = {
            "n": len(sample),
            "passed_by_round": [
                sum(r["pass"] for r in sample if r["repeat"] == n) for n in (1, 2, 3)
            ],
            "tokens_in": sum(r["tokens_in"] for r in sample),
            "tokens_out": sum(r["tokens_out"] for r in sample),
            "median_seconds": statistics.median(r["seconds"] for r in sample),
            "outcomes": dict(Counter(r["outcome"] for r in sample)),
        }
    (root / "summary.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(stats["all"], ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
