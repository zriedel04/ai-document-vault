import pytest
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


LAUNCH = "abcdefghijklmnopqrstuvwxyzABCDEF-_012345"


def test_health_echoes_the_launch_id_the_launcher_started_the_server_with(
    anonymous: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAULT_LAUNCH_ID", LAUNCH)
    assert anonymous.get("/api/health").json()["launch"] == LAUNCH


def test_health_has_no_launch_id_when_none_was_given(
    anonymous: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VAULT_LAUNCH_ID", raising=False)
    assert anonymous.get("/api/health").json()["launch"] is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 31,
        "a" * 129,
        LAUNCH + "\n",
        LAUNCH + " ",
        "Patient diagnosis confidential, see attached",
        LAUNCH[:-1] + "!",
    ],
)
def test_health_does_not_echo_a_value_of_another_shape(
    anonymous: TestClient, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("VAULT_LAUNCH_ID", value)
    assert anonymous.get("/api/health").json()["launch"] is None
