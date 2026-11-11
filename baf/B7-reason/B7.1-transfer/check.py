import json
import sys
from pathlib import Path

path = Path(sys.argv[1]) / "answer.txt"
ok = path.is_file() and path.read_text().strip() == "yes 20"
print(json.dumps({"pass": ok, "reason": "ok" if ok else "Неверный ответ"}))
