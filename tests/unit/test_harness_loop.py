import asyncio
import copy
import hashlib
import json
import tomllib
from pathlib import Path

from agent_harness.loop import SYSTEM, Engine
from agent_harness.tools import failure
from contracts.v0 import Success

CONFIG = tomllib.loads(Path("services/harness/config.toml").read_text())


def reply(text=None, calls=(), finish_reason=None):
    return {
        "model": "scripted",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "choices": [
            {
                "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
                "message": {"role": "assistant", "content": text, "tool_calls": list(calls)},
            }
        ],
    }


def call(identifier, name, **arguments):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class Model:
    def __init__(self, *replies):
        self.replies, self.histories = list(replies), []

    async def complete(self, history, tools, max_tokens):
        self.histories.append(copy.deepcopy(history))
        return self.replies.pop(0)


class Sandbox:
    def __init__(self):
        self.files, self.operations = {"a.txt": "old"}, []
        self.running = asyncio.Event()
        self.cancelled = False

    async def execute(self, operation, identifier, timeout, emit):
        self.operations.append(operation)
        if operation.kind == "ReadFile":
            if operation.path not in self.files:
                return failure("sandbox", "not_found")
            content = self.files[operation.path]
            return Success(
                status="ok",
                value={"content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()},
            )
        if operation.kind == "WriteFile":
            self.files[operation.path] = operation.content
            return Success(status="ok")
        if operation.command == "wait":
            self.running.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return failure("command", "nonzero_exit")


def run(model, sandbox, decisions=(), config=None):
    history = [{"role": "system", "content": SYSTEM}]
    previews, events = [], []
    decisions = iter(decisions)

    async def approve(identifier, data):
        previews.append(data)
        return next(decisions)

    result = asyncio.run(
        Engine(model, sandbox, config or CONFIG).run(
            history, "task", approve, lambda event, data: events.append((event, data))
        )
    )
    return result, history, previews, events


def test_text_without_tools_finishes_and_usage_is_recorded():
    model, sandbox = Model(reply("Готово")), Sandbox()
    result, history, previews, _ = run(model, sandbox)
    assert result.status == "ok" and result.value["message"] == "Готово"
    assert result.value["usage"][0]["prompt_tokens"] == 10
    assert [item["role"] for item in history] == ["system", "user", "assistant"]
    assert sandbox.operations == previews == []


def test_read_write_diff_approval_and_command_error_in_context():
    sha = hashlib.sha256(b"old").hexdigest()
    model = Model(
        reply(calls=[call("r", "read_file", path="a.txt")]),
        reply(calls=[call("w", "write_file", path="a.txt", content="new", expected_sha256=sha)]),
        reply(calls=[call("c", "run_command", command="pytest")]),
        reply("Тесты упали"),
    )
    sandbox = Sandbox()
    result, _, previews, events = run(model, sandbox, [True, True])
    assert result.status == "ok" and sandbox.files["a.txt"] == "new"
    assert "-old" in previews[0]["diff"] and "+new" in previews[0]["diff"]
    assert previews[1]["operation"]["command"] == "pytest"
    tool_output = json.loads(model.histories[-1][-1]["content"])
    assert tool_output["error"]["kind"] == "command"
    assert sum(item.kind == "WriteFile" for item in sandbox.operations) == 1
    assert any(data.get("usage") for _, data in events)


def test_denial_is_a_tool_result_and_does_not_execute_command():
    model = Model(
        reply(calls=[call("c", "run_command", command="touch bad")]), reply("Отказ принят")
    )
    sandbox = Sandbox()
    result, _, previews, _ = run(model, sandbox, [False])
    assert result.status == "ok" and len(previews) == 1
    assert sandbox.operations == []
    assert json.loads(model.histories[-1][-1]["content"])["error"]["code"] == "approval_denied"


def test_stale_write_is_rejected_before_approval():
    model = Model(
        reply(
            calls=[call("w", "write_file", path="a.txt", content="new", expected_sha256="0" * 64)]
        ),
        reply("Конфликт"),
    )
    sandbox = Sandbox()
    _, _, previews, _ = run(model, sandbox)
    assert previews == [] and sandbox.files["a.txt"] == "old"
    assert json.loads(model.histories[-1][-1]["content"])["error"]["code"] == "write_conflict"


def test_invalid_tool_is_returned_to_model_without_execution():
    model = Model(
        reply(calls=[call("bad", "read_file", path="../outside")]), reply("Исправлю запрос")
    )
    sandbox = Sandbox()
    run(model, sandbox)
    assert sandbox.operations == []
    assert json.loads(model.histories[-1][-1]["content"])["error"]["code"] == "invalid_tool_call"


def test_model_limit_and_truncated_reply_are_not_success():
    model = Model(reply(calls=[call("r", "read_file", path="a.txt")]))
    result, *_ = run(model, Sandbox(), config=CONFIG | {"max_model_calls": 1})
    assert result.error.kind == "limit"
    result, *_ = run(Model(reply("unfinished", finish_reason="length")), Sandbox())
    assert result.error.code == "invalid_model_reply"


def test_cancel_closes_remaining_tool_calls_and_stops_execution():
    async def scenario():
        sandbox = Sandbox()
        model = Model(
            reply(
                calls=[
                    call("one", "run_command", command="wait"),
                    call("two", "run_command", command="never"),
                ]
            )
        )
        history = [{"role": "system", "content": SYSTEM}]

        async def approve(*_):
            return True

        task = asyncio.create_task(
            Engine(model, sandbox, CONFIG).run(history, "task", approve, lambda *_: None)
        )
        await asyncio.wait_for(sandbox.running.wait(), 1)
        task.cancel()
        result = await task
        assert result.status == "cancelled" and sandbox.cancelled
        assert [item["tool_call_id"] for item in history if item["role"] == "tool"] == [
            "one",
            "two",
        ]
        assert len(sandbox.operations) == 1

    asyncio.run(scenario())


def test_approval_wait_has_its_own_timeout_and_does_not_execute():
    async def scenario():
        sandbox = Sandbox()
        model = Model(reply(calls=[call("c", "run_command", command="never")]))

        async def approve(*_):
            await asyncio.Event().wait()

        result = await Engine(model, sandbox, CONFIG | {"approval_timeout_s": 0.01}).run(
            [], "task", approve, lambda *_: None
        )
        assert result.error.kind == "timeout" and sandbox.operations == []

    asyncio.run(scenario())
