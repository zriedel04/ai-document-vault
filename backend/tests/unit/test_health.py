from fastapi.testclient import TestClient

from vault.core.config import Settings
from vault.main import create_app


def test_health_reports_ok_and_version() -> None:
    client = TestClient(create_app(Settings()))
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_unknown_route_is_404() -> None:
    client = TestClient(create_app(Settings()))
    assert client.get("/api/nothing").status_code == 404


def test_openapi_schema_is_served_for_the_client_generator() -> None:
    client = TestClient(create_app(Settings()))
    schema = client.get("/api/openapi.json").json()
    assert "/api/health" in schema["paths"]
