import json
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from contracts.export import schemas
from contracts.v0 import SandboxMessage


def request():
    return {
        "version": "0",
        "type": "exec",
        "session_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "operation": {"kind": "ReadFile", "path": "src/a.py"},
    }


def test_exported_schemas_match_models():
    for name, schema in schemas():
        Draft202012Validator.check_schema(schema)
        assert json.loads((Path("contracts") / name).read_text()) == schema


@pytest.mark.parametrize(
    "path", ["/tmp/a", "a/../b", "a//b", "a/", ".git/config", "a/._are_x", "a\\b", "a\x00b"]
)
def test_python_and_json_schema_reject_unsafe_paths(path):
    body = request()
    body["operation"]["path"] = path
    with pytest.raises(ValidationError):
        TypeAdapter(SandboxMessage).validate_python(body)
    schema = dict(schemas())["harness-sandbox.v0.schema.json"]
    assert list(Draft202012Validator(schema).iter_errors(body))


@pytest.mark.parametrize("change", ["version", "type", "extra", "operation"])
def test_envelope_is_versioned_and_closed(change):
    body = request()
    if change == "extra":
        body["mount"] = "/"
    elif change == "operation":
        body["operation"]["kind"] = "Unknown"
    else:
        del body[change]
    with pytest.raises(ValidationError):
        TypeAdapter(SandboxMessage).validate_python(body)


def test_request_and_final_error_validate_in_both_forms():
    body = request()
    event = {
        "version": "0",
        "type": "result",
        "session_id": body["session_id"],
        "operation_id": body["operation_id"],
        "sequence": 2,
        "result": {
            "status": "error",
            "value": {"exit_code": 1},
            "error": {"kind": "command", "code": "nonzero_exit", "message": "Command failed"},
        },
    }
    schema = dict(schemas())["harness-sandbox.v0.schema.json"]
    for message in (body, event):
        Draft202012Validator(schema).validate(message)
        TypeAdapter(SandboxMessage).validate_python(message)
