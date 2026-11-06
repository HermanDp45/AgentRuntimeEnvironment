from fastapi.testclient import TestClient

from agent_runtime.gateway import app


def test_health_contract():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
