from fastapi.testclient import TestClient


def test_health_reports_ok_and_version(anonymous: TestClient) -> None:
    response = anonymous.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_unknown_route_is_404_for_a_signed_in_client(client: TestClient) -> None:
    assert client.get("/api/nothing").status_code == 404


def test_openapi_schema_is_served_for_the_client_generator(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    assert "/api/health" in schema["paths"]
    assert "/api/session" in schema["paths"]
