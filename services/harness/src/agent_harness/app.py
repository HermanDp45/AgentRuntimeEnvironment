import asyncio
import json
import os
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx2
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agent_harness.clients import ModelClient
from agent_harness.loop import Stop
from agent_harness.runtime import Rejected, Runtime
from contracts.v0 import (
    Approval,
    ClientCancel,
    ClientClose,
    ClientSession,
    ClientTask,
    FixtureSession,
    Released,
    Session,
)


def create_app(runtime=None):
    @asynccontextmanager
    async def lifespan(app):
        async with (
            httpx2.AsyncClient(
                base_url=os.getenv("LLM_URL", "http://llm-proxy:8000"), timeout=65, trust_env=False
            ) as model_http,
            httpx2.AsyncClient(
                base_url=os.getenv("SANDBOX_URL", "http://sandbox-manager:8081"),
                timeout=httpx2.Timeout(15, connect=5),
                trust_env=False,
            ) as sandbox_http,
        ):
            config = (
                tomllib.loads(Path(os.getenv("HARNESS_CONFIG", "/app/config.toml")).read_text())
                if runtime is None
                else runtime.config
            )
            app.state.runtime = runtime or Runtime(
                ModelClient(model_http, config),
                sandbox_http,
                config,
                os.getenv("HARNESS_TRACES", "/runs"),
            )
            try:
                yield
            finally:
                await app.state.runtime.close()

    app = FastAPI(title="Agent harness", version="0", lifespan=lifespan)

    @app.exception_handler(Rejected)
    async def rejected(_, error):
        return JSONResponse(
            status_code=error.status,
            content={
                "version": "0",
                "type": "error",
                "error": {
                    "kind": "sandbox",
                    "code": error.code,
                    "message": error.code.replace("_", " "),
                },
            },
        )

    @app.exception_handler(Stop)
    async def stopped(_, error):
        return JSONResponse(
            status_code=503,
            content={"version": "0", "type": "error", "error": error.result.error.model_dump()},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(_, error):
        return JSONResponse(
            status_code=422,
            content={
                "version": "0",
                "type": "error",
                "error": {
                    "kind": "sandbox",
                    "code": "invalid_request",
                    "message": "Request does not match v0",
                },
            },
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v0/sessions", response_model=Session)
    async def provision(body: FixtureSession | ClientSession, request: Request):
        await request.app.state.runtime.provision(body)
        return Session(
            version="0", type="session", session_id=body.session_id, read_only=body.read_only
        )

    @app.post("/v0/tasks", status_code=202)
    async def task(body: ClientTask, request: Request):
        return (await request.app.state.runtime.start(body)).state()

    @app.get("/v0/runs/{identifier}")
    async def state(identifier: UUID, request: Request):
        return request.app.state.runtime.run(identifier).state()

    @app.get("/v0/runs/{identifier}/events")
    async def events(
        identifier: UUID,
        request: Request,
        after: int = Query(0, ge=0),
        last_event_id: str | None = Header(None),
    ):
        run = request.app.state.runtime.run(identifier)
        try:
            after = max(after, int(last_event_id)) if last_event_id is not None else after
        except ValueError:
            raise Rejected("invalid_event_id", 422) from None
        if not 0 <= after <= len(run.events):
            raise Rejected("invalid_event_id", 422)

        async def stream():
            cursor = after
            while True:
                run.changed.clear()
                for event in run.events[cursor:]:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
                if run.result is not None:
                    return
                try:
                    await asyncio.wait_for(run.changed.wait(), timeout=10)
                except TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v0/approvals")
    async def approve(body: Approval, request: Request):
        return request.app.state.runtime.approve(body).state()

    @app.post("/v0/cancel")
    async def cancel(body: ClientCancel, request: Request):
        return (await request.app.state.runtime.cancel(body)).state()

    @app.get("/v0/sessions/{identifier}/files")
    async def collect(identifier: UUID, request: Request):
        return await request.app.state.runtime.collect(identifier)

    @app.get("/v0/sessions/{identifier}/trace")
    async def trace(identifier: UUID, request: Request):
        runtime = request.app.state.runtime
        runtime.session(identifier)
        path = runtime.trace_dir / f"{identifier}.jsonl"
        return Response(
            path.read_text() if path.exists() else "", media_type="application/x-ndjson"
        )

    @app.post("/v0/close", response_model=Released)
    async def close(body: ClientClose, request: Request):
        await request.app.state.runtime.release(body.session_id)
        return Released(version="0", type="released", session_id=body.session_id)

    return app


app = create_app()
