"""A request that fails validation is a 422 in fixed text: no value, no message from pydantic."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import pytest
from fastapi import Cookie, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from vault.api import errors
from vault.core.config import Settings
from vault.main import create_app

from ..conftest import LOCAL

# Looks like a value from a private library: a name, a diagnosis and an instruction.
CANARY = "Patient-Smith-Diagnosis-Canary"
INVALID = {"msg": "not valid", "type": "invalid"}
MISSING = {"msg": "a required value is missing", "type": "missing"}


def server_log(caplog: pytest.LogCaptureFixture) -> str:
    """Every record but the test client's own (it logs the address it sends, query and all)."""
    return "\n".join(
        f"{record.name} {record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for record in caplog.records
        if not record.name.startswith("httpx")
    )


def assert_private(response: Any, caplog: pytest.LogCaptureFixture) -> None:
    assert response.status_code == 422
    assert CANARY not in response.text
    assert CANARY.lower() not in response.text.lower()
    assert CANARY not in server_log(caplog)
    for entry in response.json()["detail"]:
        assert set(entry) == {"loc", "msg", "type"}
        assert {"msg": entry["msg"], "type": entry["type"]} in (INVALID, MISSING)


@pytest.fixture(autouse=True)
def _debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)


@pytest.mark.parametrize(
    ("params", "loc"),
    [
        ({"sort": CANARY}, ["query", "sort"]),
        ({"order": CANARY}, ["query", "order"]),
        ({"limit": CANARY}, ["query", "limit"]),
        ({"offset": CANARY}, ["query", "offset"]),
        ({"trash": CANARY}, ["query", "trash"]),
        ({"limit": "9" * 400}, ["query", "limit"]),
        ({"offset": "1e999"}, ["query", "offset"]),
    ],
)
def test_query_values_are_never_echoed(
    client: TestClient, caplog: pytest.LogCaptureFixture, params: dict[str, str], loc: list[str]
) -> None:
    response = client.get("/api/documents", params=params)
    assert_private(response, caplog)
    assert response.json() == {"detail": [{"loc": loc, **INVALID}]}


def test_a_window_value_on_a_document_is_never_echoed(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.get(f"/api/documents/{CANARY}/pages", params={"limit": CANARY})
    assert_private(response, caplog)
    assert response.json() == {"detail": [{"loc": ["query", "limit"], **INVALID}]}


@pytest.mark.parametrize(
    ("body", "locs"),
    [
        ({"query": CANARY, "types": [CANARY]}, [["body", "types"]]),
        ({"query": CANARY, "types": ["txt", CANARY, CANARY]}, [["body", "types"]]),
        ({"query": [CANARY]}, [["body", "query"]]),
        ({"query": {CANARY: CANARY}}, [["body", "query"]]),
        ({"query": "a", "label": [CANARY]}, [["body", "label"]]),
        ({"query": "a", "limit": CANARY}, [["body", "limit"]]),
        ({"query": "a", "limit": 10**400}, [["body", "limit"]]),
        ({"query": "a", "offset": 1_000_000_001}, [["body", "offset"]]),
        ({"query": "a", "offset": 1.5}, [["body", "offset"]]),
        ({"query": "a", "types": [CANARY] * 11}, [["body", "types"]]),
        ({CANARY: CANARY}, [["body", "query"]]),
        ([CANARY], [["body"]]),
        (CANARY, [["body"]]),
        ({"query": 12, "label": [CANARY]}, [["body", "query"], ["body", "label"]]),
    ],
)
def test_body_values_and_keys_are_never_echoed(
    client: TestClient, caplog: pytest.LogCaptureFixture, body: Any, locs: list[list[str]]
) -> None:
    response = client.post("/api/search", json=body)
    assert_private(response, caplog)
    kind = MISSING if body == {CANARY: CANARY} else INVALID
    assert response.json() == {"detail": [{"loc": loc, **kind} for loc in locs]}


@pytest.mark.parametrize(
    "content",
    [CANARY, '{"query": "' + CANARY, '{"query": "' + CANARY + '",}', "\xff" + CANARY],
)
def test_a_body_that_is_not_json_is_never_echoed(
    client: TestClient, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    response = client.post(
        "/api/search",
        content=content.encode("utf-8", "surrogateescape"),
        headers={"Content-Type": "application/json"},
    )
    assert_private(response, caplog)
    assert response.json() == {"detail": [{"loc": ["body"], **INVALID}]}


def test_an_unknown_body_field_is_ignored_and_never_echoed(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post("/api/search", json={"query": "a", CANARY: CANARY})
    assert response.status_code == 200
    assert CANARY not in response.text
    assert CANARY not in server_log(caplog)


def test_a_missing_header_names_only_the_header(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post(
        "/api/imports/upload",
        content=CANARY.encode(),
        headers={"Content-Type": "application/octet-stream", "X-Vault-Batch": CANARY},
    )
    assert_private(response, caplog)
    assert response.json() == {"detail": [{"loc": ["header", "x-vault-filename"], **MISSING}]}


def test_routes_with_their_own_422_keep_their_fixed_text(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post("/api/search", json={"query": CANARY * 40})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert CANARY not in response.text
    assert CANARY not in server_log(caplog)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    count: int = 0
    tags: dict[str, int] = {}


class Outer(BaseModel):
    inner: Strict


def probe_app() -> FastAPI:
    """Every kind of parameter and body the vault's routes could declare, behind the handler."""
    app = FastAPI()
    errors.install(app)

    @app.get("/items/{item_id}")
    def read(
        item_id: int,
        size: Annotated[int, Query(le=10)],
        x_probe: Annotated[int, Header()],
        session: Annotated[int, Cookie()],
    ) -> dict[str, int]:
        return {"item_id": item_id}

    @app.post("/strict")
    def strict(body: Strict) -> dict[str, str]:
        return {"name": body.name}

    @app.post("/nested")
    def nested(body: Outer) -> dict[str, str]:
        return {"name": body.inner.name}

    return app


@pytest.fixture
def probe() -> TestClient:
    return TestClient(probe_app(), base_url=LOCAL)


def test_every_kind_of_parameter_names_only_itself(
    probe: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = probe.get(
        f"/items/{CANARY}",
        params={"size": CANARY},
        headers={"X-Probe": CANARY, "Cookie": f"session={CANARY}"},
    )
    assert_private(response, caplog)
    assert response.json() == {
        "detail": [
            {"loc": ["path", "item_id"], **INVALID},
            {"loc": ["query", "size"], **INVALID},
            {"loc": ["header", "x-probe"], **INVALID},
            {"loc": ["cookie", "session"], **INVALID},
        ]
    }


def test_missing_parameters_say_missing(
    probe: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = probe.get("/items/1")
    assert_private(response, caplog)
    assert response.json() == {
        "detail": [
            {"loc": ["query", "size"], **MISSING},
            {"loc": ["header", "x-probe"], **MISSING},
            {"loc": ["cookie", "session"], **MISSING},
        ]
    }


def test_a_number_over_the_limit_is_not_echoed(
    probe: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = probe.get(
        "/items/1",
        params={"size": "1" + "0" * 40},
        headers={"X-Probe": "1", "Cookie": "session=1"},
    )
    assert_private(response, caplog)
    assert response.json() == {"detail": [{"loc": ["query", "size"], **INVALID}]}


def test_a_name_the_client_made_up_is_never_named(
    probe: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown key, a dictionary key and a list index come from the request, not the code."""
    response = probe.post("/strict", json={"name": "a", CANARY: 1, "tags": {CANARY: CANARY}})
    assert_private(response, caplog)
    assert response.json() == {
        "detail": [{"loc": ["body", "tags"], **INVALID}, {"loc": ["body"], **INVALID}]
    }
    nested = probe.post("/nested", json={"inner": {"name": CANARY, CANARY: CANARY}})
    assert_private(nested, caplog)
    assert nested.json() == {"detail": [{"loc": ["body", "inner"], **INVALID}]}


def test_a_location_the_handler_does_not_know_is_left_out() -> None:
    raw = [
        {"type": "value_error", "loc": (CANARY, CANARY), "msg": CANARY, "input": CANARY},
        {"type": CANARY, "loc": ("body", CANARY), "msg": CANARY, "input": CANARY},
        {"type": "missing", "loc": (), "msg": CANARY},
    ]
    body = errors.fixed_errors(RequestValidationError(raw), route=None).model_dump()
    assert CANARY not in json.dumps(body)
    assert body == {
        "detail": [{"loc": [], **INVALID}, {"loc": ["body"], **INVALID}, {"loc": [], **MISSING}]
    }


def test_the_schema_documents_the_body_that_is_sent() -> None:
    schema = create_app(Settings(), serve_frontend=False).openapi()
    schemas = schema["components"]["schemas"]
    assert set(schemas["ValidationError"]["properties"]) == {"loc", "msg", "type"}
    assert schemas["ValidationError"]["properties"]["type"]["enum"] == ["missing", "invalid"]
    assert schemas["HTTPValidationError"]["required"] == ["detail"]
    # Every 422 the routes document still points at it.
    refs = {
        response["content"]["application/json"]["schema"]["$ref"]
        for path in schema["paths"].values()
        for operation in path.values()
        for code, response in operation.get("responses", {}).items()
        if code == "422" and "content" in response
    }
    assert refs == {"#/components/schemas/HTTPValidationError"}
