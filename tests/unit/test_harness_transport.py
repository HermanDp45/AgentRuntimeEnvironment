import asyncio
import json
import tomllib
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest

from agent_harness.clients import SandboxClient
from agent_harness.runtime import Rejected, Runtime
from contracts.v0 import Approval, ClientCancel, ClientTask, FixtureSession, ReadFile

CONFIG = tomllib.loads(Path("services/harness/config.toml").read_text())


@pytest.mark.parametrize("lost_post", [False, True])
def test_transport_recovery_observes_same_operation_without_reexecution(lost_post):
    async def scenario():
        session, operation = uuid4(), uuid4()
        posted, streams = [], []
        common = {"version": "0", "session_id": str(session), "operation_id": str(operation)}
        started = {**common, "type": "started", "sequence": 1}
        result = {
            **common,
            "type": "result",
            "sequence": 2,
            "result": {"status": "ok", "value": {"content": "fixture"}},
        }

        class BrokenStream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield ("data: " + json.dumps(started) + "\n\n").encode()
                raise httpx2.ReadError("Connection lost")

        def handler(request):
            if request.method == "POST":
                posted.append(request)
                if lost_post:
                    raise httpx2.ReadTimeout("Response lost after acceptance")
                return httpx2.Response(202, json={})
            if request.url.path.endswith("events"):
                streams.append(request.headers["Last-Event-ID"])
                if len(streams) == 1:
                    return httpx2.Response(200, stream=BrokenStream())
                return httpx2.Response(200, text="data: " + json.dumps(result) + "\n\n")
            return httpx2.Response(200, json={**common, "type": "operation", "state": "running"})

        async with httpx2.AsyncClient(
            base_url="http://sandbox", transport=httpx2.MockTransport(handler)
        ) as http:
            value = await SandboxClient(http, session).execute(
                ReadFile(kind="ReadFile", path="note.txt"), operation, 5, lambda *_: None
            )
        assert value.status == "ok" and value.value["content"] == "fixture"
        assert len(posted) == 1 and streams == ["0", "1"]

    asyncio.run(scenario())


def test_failed_cancel_blocks_session_and_never_reports_successful_cancellation(tmp_path):
    async def scenario():
        session, identifier = uuid4(), uuid4()
        executing = asyncio.Event()
        common = {}

        class WaitingStream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                executing.set()
                yield (
                    "data: " + json.dumps({**common, "type": "started", "sequence": 1}) + "\n\n"
                ).encode()
                await asyncio.Event().wait()

        def handler(request):
            if request.url.path == "/v0/exec":
                body = json.loads(request.content)
                common.update({key: body[key] for key in ("version", "session_id", "operation_id")})
                return httpx2.Response(202, json={})
            if request.url.path.endswith("events"):
                return httpx2.Response(200, stream=WaitingStream())
            if request.url.path == "/v0/cancel":
                return httpx2.Response(503, json={"error": "Docker unavailable"})
            return httpx2.Response(200, json={})

        class Model:
            async def complete(self, *_):
                return {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "one",
                                        "type": "function",
                                        "function": {
                                            "name": "run_command",
                                            "arguments": '{"command":"sleep 300"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                }

        async with httpx2.AsyncClient(
            base_url="http://sandbox", transport=httpx2.MockTransport(handler)
        ) as http:
            runtime = Runtime(Model(), http, CONFIG, tmp_path)
            try:
                await runtime.provision(
                    FixtureSession(version="0", type="session.fixture", session_id=session)
                )
                request = ClientTask(
                    version="0", type="task", session_id=session, run_id=identifier, message="task"
                )
                run = await runtime.start(request)
                async with asyncio.timeout(1):
                    while run.pending is None:
                        await asyncio.sleep(0)
                runtime.approve(
                    Approval(
                        version="0",
                        type="approval",
                        session_id=session,
                        run_id=identifier,
                        operation_id=run.pending[0],
                        approved=True,
                    )
                )
                await asyncio.wait_for(executing.wait(), 1)
                await runtime.cancel(
                    ClientCancel(
                        version="0", type="run.cancel", session_id=session, run_id=identifier
                    )
                )
                assert run.result.status == "error" and run.result.error.kind == "transport"
                assert run.result.error.code == "sandbox_outcome_unknown"
                assert sum(event["type"] == "run.result" for event in run.events) == 1
                with pytest.raises(Rejected) as error:
                    await runtime.start(request.model_copy(update={"run_id": uuid4()}))
                assert error.value.code == "sandbox_outcome_unknown"
            finally:
                await runtime.close()

    asyncio.run(scenario())
