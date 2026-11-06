"""Ключ и адрес провайдера известны только этому сервису."""

import json
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[dict[str, JsonValue]] = Field(min_length=1, max_length=512)
    tools: list[dict[str, JsonValue]] = Field(max_length=16)
    tool_choice: str = "auto"
    max_tokens: int = Field(default=1024, ge=1, le=2048)
    temperature: float = Field(default=0, ge=0, le=2)


def error(status, code):
    return JSONResponse(
        status_code=status,
        content={
            "version": "0",
            "type": "error",
            "error": {"kind": "transport", "code": code, "message": code.replace("_", " ")},
        },
    )


def create_app(client=None):
    provider = os.getenv("LLM_PROVIDER_URL", "https://inference.airi.net").rstrip("/")
    model = os.getenv("LLM_MODEL", "Qwen/Qwen3-235B-A22B-Instruct-2507")
    key = os.getenv("LLM_API_KEY", "")

    @asynccontextmanager
    async def lifespan(app):
        url = urlsplit(provider)
        if (
            url.username
            or url.password
            or not url.netloc
            or not (
                url.scheme == "https"
                or url.scheme == "http"
                and url.hostname in ("mock-llm", "127.0.0.1", "localhost")
            )
        ):
            raise ValueError("HTTPS provider URL required")
        async with httpx2.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as http:
            app.state.http = client or http
            yield

    app = FastAPI(title="LLM proxy", version="0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid(_, exc):
        return error(422, "invalid_request")

    @app.get("/health")
    async def health():
        return {"status": "ok", "configured": bool(key)}

    @app.post("/v1/chat/completions")
    async def chat(body: Chat):
        if not key:
            return error(503, "api_key_missing")
        payload = {**body.model_dump(), "model": model, "stream": False}
        if len(json.dumps(payload).encode()) > 2 * 1024 * 1024:
            return error(413, "context_too_large")
        try:
            async with app.state.http.stream(
                "POST",
                provider + "/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        return error(502, "provider_reply_too_large")
                return json.loads(data.decode().replace(key, "REDACTED"))
        except (httpx2.HTTPError, ValueError):
            return error(502, "provider_unavailable")

    return app


app = create_app()
