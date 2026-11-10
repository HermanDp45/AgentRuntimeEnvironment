import json
import sys
from pathlib import Path

answer = Path(sys.argv[1]) / "answer.txt"
ok = answer.is_file() and answer.read_text(encoding="utf-8").strip() == "17"
print(json.dumps({"pass": ok, "reason": "ok" if ok else "Медиана отсутствует или неверна"}))
