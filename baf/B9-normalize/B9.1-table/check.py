import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
source = Path(__file__).parent
try:
    actual = json.loads((root / "result.json").read_text())
    expected = json.loads((source / "expected.json").read_text())
    ok = (
        json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
        and (root / "input.csv").read_bytes() == (source / "fixture/input.csv").read_bytes()
    )
except (OSError, ValueError):
    ok = False
print(
    json.dumps({"pass": ok, "reason": "ok" if ok else "Неверная нормализация или изменён источник"})
)
