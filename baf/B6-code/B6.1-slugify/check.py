import ast
import json
import sys
from pathlib import Path

# Код агента здесь не исполняется на хосте: скрытые проверки идут в сандбоксе.
path = Path(sys.argv[1]) / "slug.py"
try:
    tree = ast.parse(path.read_text())
    ok = any(isinstance(node, ast.FunctionDef) and node.name == "slugify" for node in tree.body)
except (OSError, SyntaxError):
    ok = False
print(json.dumps({"pass": ok, "reason": "ok" if ok else "Нет функции slugify"}))
