import asyncio

import httpx2
from fastapi.testclient import TestClient

from llm_proxy.app import create_app


def test_only_proxy_sends_key_and_error_body_is_not_forwarded(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "private-provider-key")
    monkeypatch.setenv("LLM_PROVIDER_URL", "https://provider.test")
    received = []

    def provider(request):
        received.append(request)
        return httpx2.Response(401, json={"detail": "private-provider-key and private input"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(provider))
    with TestClient(create_app(client)) as app:
        response = app.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "task"}], "tools": []},
        )
    asyncio.run(client.aclose())
    assert response.status_code == 502
    assert received[0].headers["Authorization"] == "Bearer private-provider-key"
    assert "private-provider-key" not in response.text and "private input" not in response.text


def test_missing_key_never_contacts_provider(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with TestClient(create_app()) as app:
        response = app.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "task"}], "tools": []},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "api_key_missing"


def test_chat_profiles_isolate_keys_and_pin_model(monkeypatch):
    import json

    monkeypatch.setenv("LLM_PROVIDER_URL", "https://inference.airi.net")
    monkeypatch.setenv("LLM_API_KEY", "airi-key")
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "course-key")
    received = []

    def provider(request):
        body = json.loads(request.content)
        received.append((request.url.host, request.headers["Authorization"], body["model"]))
        assert "model_profile" not in body
        return httpx2.Response(200, json={"model": body["model"]})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(provider))
    body = {"messages": [{"role": "user", "content": "test"}], "tools": []}
    with TestClient(create_app(client)) as app:
        for profile in ("deepseek", "qwen"):
            assert app.post(
                "/v1/chat/completions", json={**body, "model_profile": profile}
            ).is_success
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with TestClient(create_app(client)) as app:
        assert (
            app.post("/v1/chat/completions", json={**body, "model_profile": "deepseek"}).status_code
            == 503
        )
    asyncio.run(client.aclose())
    assert received == [
        (
            "litellm-backend.deeppavlov.ai",
            "Bearer course-key",
            "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        ),
        ("inference.airi.net", "Bearer airi-key", "Qwen/Qwen3-235B-A22B-Instruct-2507"),
    ]
