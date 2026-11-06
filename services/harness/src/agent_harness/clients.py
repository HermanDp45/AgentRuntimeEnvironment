"""HTTP к прокси модели и контракту сандбокса; команды на хосте не исполняются."""

import asyncio
import json
import time

import httpx2
from pydantic import TypeAdapter

from agent_harness.loop import Stop
from agent_harness.tools import failure
from contracts.v0 import Event, OperationState


async def json_request(client, method, path, **kwargs):
    try:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx2.HTTPError, ValueError):
        raise Stop(failure("transport", "upstream_unavailable")) from None


class ModelClient:
    def __init__(self, client, config):
        self.client, self.config = client, config

    async def complete(self, history, tools, max_tokens):
        try:
            async with self.client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "messages": history,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_tokens": max_tokens,
                    "temperature": 0,
                },
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.config["max_reply_bytes"]:
                        raise Stop(failure("model", "model_reply_too_large"))
                return json.loads(data)
        except (httpx2.HTTPError, ValueError):
            raise Stop(failure("transport", "llm_unavailable")) from None


class SandboxClient:
    def __init__(self, client, session_id):
        self.client, self.session_id = client, session_id
        self.uncertain = False

    async def cancel(self, operation_id):
        return await json_request(
            self.client,
            "POST",
            "/v0/cancel",
            json={"version": "0", "type": "cancel", "operation_id": str(operation_id)},
        )

    async def execute(self, operation, operation_id, timeout_s, emit):
        path = f"/v0/operations/{operation_id}"
        sequence, result = 0, None
        try:
            try:
                response = await self.client.post(
                    "/v0/exec",
                    json={
                        "version": "0",
                        "type": "exec",
                        "session_id": str(self.session_id),
                        "operation_id": str(operation_id),
                        "operation": operation.model_dump(),
                        "timeout_s": timeout_s,
                    },
                )
                response.raise_for_status()
            except httpx2.HTTPError:
                # При потере ответа POST наблюдаем тот же ID, не создаём новую операцию.
                state = await json_request(self.client, "GET", path)
                self._state(state, operation_id)
            deadline = time.monotonic() + timeout_s + 10
            for attempt in range(3):
                try:
                    async with self.client.stream(
                        "GET", path + "/events", headers={"Last-Event-ID": str(sequence)}
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if time.monotonic() > deadline:
                                raise TimeoutError
                            if not line.startswith("data: "):
                                continue
                            event = TypeAdapter(Event).validate_json(line[6:])
                            if (
                                event.session_id != self.session_id
                                or event.operation_id != operation_id
                            ):
                                raise ValueError("Wrong operation in stream")
                            if event.sequence <= sequence:
                                continue
                            if event.sequence != sequence + 1:
                                raise ValueError("Missing event")
                            sequence = event.sequence
                            if event.type == "result":
                                result = event.result
                            elif event.type in ("stdout", "stderr"):
                                emit(
                                    "tool",
                                    {
                                        "phase": event.type,
                                        "operation_id": str(operation_id),
                                        "text": event.text,
                                    },
                                )
                    if result is not None:
                        self.uncertain = (
                            result.status == "error" and result.error.kind == "transport"
                        )
                        return result
                except httpx2.HTTPError:
                    if attempt == 2:
                        raise
                state = self._state(await json_request(self.client, "GET", path), operation_id)
                if state.result is not None:
                    self.uncertain = (
                        state.result.status == "error" and state.result.error.kind == "transport"
                    )
                    return state.result
            raise ValueError("No result")
        except (asyncio.CancelledError, TimeoutError):
            await self._stop(operation_id)
            raise
        except (httpx2.HTTPError, ValueError, Stop):
            await self._stop(operation_id)
            raise Stop(failure("transport", "sandbox_outcome_unknown")) from None

    async def _stop(self, operation_id):
        task = asyncio.create_task(self.cancel(operation_id))
        try:
            async with asyncio.timeout(15):
                state = self._state(await asyncio.shield(task), operation_id)
            self.uncertain = (
                state.state != "finished"
                or state.result is None
                or (state.result.status == "error" and state.result.error.kind == "transport")
            )
        except (Stop, TimeoutError, ValueError):
            self.uncertain = True
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _state(self, body, operation_id):
        state = OperationState.model_validate(body)
        if state.session_id != self.session_id or state.operation_id != operation_id:
            raise ValueError("Wrong operation in state")
        return state
