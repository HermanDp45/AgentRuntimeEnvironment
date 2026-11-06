import json
from pathlib import Path

from pydantic import TypeAdapter

from contracts.v0 import ClientMessage, SandboxMessage


def schemas():
    for name, model in (("client-server", ClientMessage), ("harness-sandbox", SandboxMessage)):
        schema = TypeAdapter(model).json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"urn:agent-runtime:{name}:v0"
        yield f"{name}.v0.schema.json", schema


if __name__ == "__main__":
    for name, schema in schemas():
        Path(__file__).with_name(name).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        )
