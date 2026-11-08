from fastapi.testclient import TestClient

from app.api import app
from app.auth import current_user


def test_arm2_status_requires_authentication_and_returns_bootstrap():
    client = TestClient(app)

    response = client.get("/api/arm2/status")
    assert response.status_code == 401

    app.dependency_overrides[current_user] = lambda: object()
    try:
        response = client.get("/api/arm2/status")
    finally:
        app.dependency_overrides.pop(current_user, None)

    assert response.status_code == 200
    assert response.json() == {"module": "arm2", "phase": "bootstrap"}
