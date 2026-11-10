import json
import sys
from pathlib import Path

from jsonschema import ValidationError, validate

schema = {
    "type": "object",
    "properties": {
        "service": {"const": "orders"},
        "retries": {"type": "integer", "const": 3},
        "enabled": {"const": True},
    },
    "required": ["service", "retries", "enabled"],
    "additionalProperties": False,
}
try:
    validate(json.loads((Path(sys.argv[1]) / "config.json").read_text(encoding="utf-8")), schema)
except (OSError, ValueError, ValidationError):
    result = {"pass": False, "reason": "config.json отсутствует или не соответствует схеме"}
else:
    result = {"pass": True, "reason": "ok"}
print(json.dumps(result))
