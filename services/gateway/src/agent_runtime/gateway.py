import hmac
import os
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask


def create_app(transport=None):
    @asynccontextmanager
    async def lifespan(app):
        async with httpx2.AsyncClient(
            base_url=os.getenv("HARNESS_URL", "http://harness:8000"),
            timeout=httpx2.Timeout(20, connect=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            app.state.client = client
            app.state.tokens = [
                token.strip()
                for token in os.getenv("CLIENT_TOKENS", "").split(",")
                if token.strip()
            ]
            yield

    app = FastAPI(title="Agent Runtime Gateway", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/v0/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request):
        def error(status, code):
            return JSONResponse(
                status_code=status,
                content={
                    "version": "0",
                    "type": "error",
                    "error": {"kind": "transport", "code": code, "message": code.replace("_", " ")},
                },
            )

        if not app.state.tokens:
            return error(503, "client_auth_not_configured")
        authorization = request.headers.get("Authorization", "")
        supplied = (
            authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        )
        if not any(
            hmac.compare_digest(supplied.encode(), token.encode()) for token in app.state.tokens
        ):
            return error(401, "unauthorized")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 10 * 1024 * 1024:
                return error(413, "request_too_large")
        headers = {"Content-Type": "application/json"}
        if identifier := request.headers.get("Last-Event-ID"):
            headers["Last-Event-ID"] = identifier
        try:
            upstream = app.state.client.build_request(
                request.method,
                "/v0/" + path,
                params=request.query_params,
                content=bytes(body),
                headers=headers,
            )
            response = await app.state.client.send(upstream, stream=True)
        except httpx2.HTTPError:
            return error(503, "harness_unavailable")
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers={
                name: value
                for name, value in response.headers.items()
                if name in ("content-type", "cache-control", "x-accel-buffering")
            },
            background=BackgroundTask(response.aclose),
        )

    return app


app = create_app()
