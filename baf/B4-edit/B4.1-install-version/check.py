import json
import sys
from pathlib import Path

original = Path(__file__).with_name("fixture") / "README.md"
expected = original.read_bytes().replace(
    b"pip install orders-cli==1.2", b"pip install orders-cli==1.3"
)
answer = Path(sys.argv[1]) / "README.md"
ok = answer.is_file() and answer.read_bytes() == expected
print(
    json.dumps({"pass": ok, "reason": "ok" if ok else "Ожидалось изменение одной строки Install"})
)
