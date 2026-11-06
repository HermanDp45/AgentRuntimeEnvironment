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
