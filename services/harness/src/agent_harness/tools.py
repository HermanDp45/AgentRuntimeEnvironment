"""Три инструмента: схема для модели, проверка вызова и подготовка diff."""

import difflib
import json

from contracts.v0 import Failed, ReadFile, RunCommand, WriteFile

SPECS = {
    "read_file": (ReadFile, "Read a UTF-8 file relative to /work; returns content and SHA-256."),
    "write_file": (
        WriteFile,
        "Write a UTF-8 file after approval. Use its SHA-256 or missing for a new file.",
    ),
    "run_command": (
        RunCommand,
        "Run a shell command in /work after approval; returns exit code and output.",
    ),
}


def failure(kind, code):
    return Failed(
        status="error", error={"kind": kind, "code": code, "message": code.replace("_", " ")}
    )


def schemas():
    tools = []
    for name, (model, description) in SPECS.items():
        parameters = model.model_json_schema()
        parameters["properties"].pop("kind")
        parameters["required"].remove("kind")
        parameters.pop("title", None)
        tools.append(
            {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            }
        )
    return tools


def parse(call):
    model, _ = SPECS[call["function"]["name"]]
    arguments = json.loads(call["function"]["arguments"])
    if not isinstance(arguments, dict) or "kind" in arguments:
        raise ValueError("Expected tool arguments without kind")
    return model.model_validate({**arguments, "kind": model.__name__})


async def preview(operation, execute):
    data = {"operation": operation.model_dump()}
    if isinstance(operation, WriteFile):
        old = await execute(ReadFile(kind="ReadFile", path=operation.path))
        if old.status == "error":
            if old.error.code != "not_found" or operation.expected_sha256 != "missing":
                return old, None
            content, sha = "", "missing"
        elif old.status == "ok":
            content, sha = old.value["content"], old.value["sha256"]
        else:
            return old, None
        if sha != operation.expected_sha256:
            return failure("sandbox", "write_conflict"), None
        data["diff"] = "\n".join(
            difflib.unified_diff(
                content.splitlines(),
                operation.content.splitlines(),
                fromfile=operation.path,
                tofile=operation.path,
                lineterm="",
            )
        )
    return None, data
