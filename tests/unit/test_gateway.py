import httpx2
from fastapi.testclient import TestClient

from agent_runtime.gateway import app, create_app


def test_health_contract():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_gateway_checks_token_and_does_not_forward_it(monkeypatch):
    monkeypatch.setenv("CLIENT_TOKENS", "local-client-token")
    received = []

    def upstream(request):
        received.append(request)
        assert request.content == b'{"task":"example"}'
        assert "authorization" not in request.headers
        return httpx2.Response(202, json={"accepted": True})

    with TestClient(create_app(httpx2.MockTransport(upstream))) as client:
        assert client.post("/v0/tasks", json={"task": "example"}).status_code == 401
        response = client.post(
            "/v0/tasks",
            content=b'{"task":"example"}',
            headers={"Authorization": "Bearer local-client-token"},
        )
    assert response.status_code == 202 and len(received) == 1
