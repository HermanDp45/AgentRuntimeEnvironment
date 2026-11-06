import json
import os
import re
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from contracts.v0 import Cancel, Collect, Collection, Exec, Provision, Release, Released, Session
from sandbox_manager.backend import DockerBackend
from sandbox_manager.runtime import Rejected, Runtime


def create_app(runtime=None):
    @asynccontextmanager
    async def lifespan(app):
        if runtime is None:
            config = tomllib.loads(
                Path(os.environ.get("SANDBOX_CONFIG", "/app/config.toml")).read_text()
            )
            config["image"] = os.environ.get("SANDBOX_IMAGE", config["image"])
            namespace = os.environ.get("SANDBOX_NAMESPACE", "local")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", namespace):
                raise ValueError("Invalid sandbox namespace")
            backend = DockerBackend(config, namespace)
            backend.health()
            backend.cleanup()
            app.state.runtime = Runtime(backend, config, os.environ.get("SANDBOX_STATE", "/state"))
        else:
            app.state.runtime = runtime
        try:
            yield
        finally:
            app.state.runtime.close()

    app = FastAPI(title="Sandbox manager", version="0", lifespan=lifespan)

    @app.exception_handler(Exception)
    async def unavailable(_, error):
        return JSONResponse(
            status_code=503,
            content={
                "version": "0",
                "type": "error",
                "error": {
                    "kind": "transport",
                    "code": "sandbox_unavailable",
                    "message": "Sandbox unavailable",
                },
            },
        )

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

    @app.exception_handler(RequestValidationError)
    async def invalid(_, error):
        # Стандартный 422 возвращает input, который может содержать текст пользовательского файла.
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
    def health(request: Request):
        request.app.state.runtime.backend.health()
        return {"status": "ok"}

    @app.post("/v0/provision", response_model=Session)
    def provision(body: Provision, request: Request):
        request.app.state.runtime.provision(body)
        return Session(
            version="0", type="session", session_id=body.session_id, read_only=body.read_only
        )

    @app.post("/v0/exec", status_code=202)
    def execute(body: Exec, request: Request):
        return request.app.state.runtime.execute(body).state()

    @app.get("/v0/operations/{identifier}")
    def state(identifier: UUID, request: Request):
        return request.app.state.runtime.operation(identifier).state()

    @app.get("/v0/operations/{identifier}/events")
    def events(
        identifier: UUID,
        request: Request,
        after: int = Query(0, ge=0),
        last_event_id: str | None = Header(None),
    ):
        operation = request.app.state.runtime.operation(identifier)
        if last_event_id is not None:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                raise Rejected("invalid_event_id", 422) from None
        if after < 0 or after > len(operation.events):
            raise Rejected("invalid_event_id", 422)

        def stream():
            cursor = after
            while True:
                with operation.condition:
                    if cursor == len(operation.events) and operation.result is None:
                        operation.condition.wait(timeout=10)
                    pending = operation.events[cursor:]
                    finished = operation.result is not None
                for event in pending:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
                if finished:
                    return
                if not pending:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v0/cancel")
    def cancel(body: Cancel, request: Request):
        return request.app.state.runtime.cancel(body.operation_id).state()

    @app.post("/v0/collect", response_model=Collection)
    def collect(body: Collect, request: Request):
        files = request.app.state.runtime.collect(body.session_id)
        return Collection(version="0", type="collection", session_id=body.session_id, files=files)

    @app.post("/v0/release", response_model=Released)
    def release(body: Release, request: Request):
        request.app.state.runtime.release(body.session_id)
        return Released(version="0", type="released", session_id=body.session_id)

    return app


app = create_app()
