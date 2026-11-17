"""Every JSON request body refuses a key it does not declare, in the fixed text of a 422."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any, get_args

import pytest
from fastapi import Body, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from starlette.routing import Route

from vault.api.labels import LabelChange, NewLabel
from vault.api.models import RequestBody
from vault.api.search import SearchRequest

from .test_validation_errors import CANARY, INVALID, assert_private

# A body each model accepts, with every field it declares, so a key it names is never refused.
# A new body model needs a row here: ``test_every_body_route_has_a_sample`` fails until it has.
SAMPLES: dict[type[BaseModel], dict[str, Any]] = {
    SearchRequest: {
        "query": "budget",
        "limit": 5,
        "offset": 0,
        "types": ["txt", "md"],
        "label": None,
        "imported_from": "2026-03-01T05:00:00Z",
        "imported_to": None,
    },
    NewLabel: {"kind": "collection", "name": "Receipts", "parent_id": None},
    LabelChange: {"name": "Receipts 2026", "parent_id": None},
}


@pytest.fixture(autouse=True)
def _debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)


def models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    """Every pydantic model ``annotation`` names, through lists, unions and ``Annotated``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in get_args(annotation):
        yield from models_in(argument)


def lenient_models(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> list[str]:
    """The names of ``model`` and of the models inside it that do not forbid unknown keys."""
    seen = set() if seen is None else seen
    if model in seen:
        return []
    seen.add(model)
    found = [] if model.model_config.get("extra") == "forbid" else [model.__name__]
    for info in model.model_fields.values():
        for inner in models_in(info.annotation):
            found += lenient_models(inner, seen)
    return found


def effective_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """Every route as the app answers it, through included routers.

    FastAPI keeps an included router as one entry of ``app.routes`` and builds each route's
    effective form (full path, dependencies, ``body_field``) from it; that form is what validates
    a request. A kind of route this walk does not know fails the test instead of being skipped."""
    for route in routes:
        if isinstance(route, APIRoute) or type(route).__name__ == "_EffectiveRouteContext":
            yield route
        elif type(route).__name__ == "_IncludedRouter":
            yield from effective_routes(route.effective_candidates())
        elif type(route) is Route:
            # A plain Starlette route (the schema, the docs page, the frontend's catch-alls for
            # unknown /api paths) has no body model: nothing in it reads a JSON body.
            continue
        else:
            pytest.fail(f"a route this walk does not know: {type(route).__name__}")


def body_routes(app: FastAPI) -> dict[str, Any]:
    """Each route with a JSON body, by method and path, with the annotation FastAPI validates.

    ``body_field`` is the body as FastAPI reads it: the model itself for a single body parameter
    (in the route or one of its dependencies), or a model FastAPI builds when there are several or
    one is embedded, which ignores unknown keys and so is refused below."""
    found: dict[str, Any] = {}
    for route in effective_routes(app.routes):
        if route.body_field is not None:
            for method in sorted(route.methods):
                found[f"{method} {route.path}"] = route.body_field.field_info.annotation
    return found


def problems(app: FastAPI) -> list[str]:
    """Each body that may accept a key it does not declare, and why."""
    found: list[str] = []
    for where, annotation in body_routes(app).items():
        if not (isinstance(annotation, type) and issubclass(annotation, RequestBody)):
            found.append(f"{where}: not a RequestBody")
            continue
        found += [f"{where}: {name} allows extra keys" for name in lenient_models(annotation)]
    return found


def test_every_body_model_forbids_unknown_keys(app: FastAPI) -> None:
    routes = body_routes(app)
    # Not vacuous: the walk reaches the included routers and finds the search body.
    assert routes.get("POST /api/search") is SearchRequest
    assert len(list(effective_routes(app.routes))) > 10
    assert problems(app) == []


def test_every_body_route_has_a_sample(app: FastAPI) -> None:
    assert set(body_routes(app).values()) == set(SAMPLES)


class Inner(BaseModel):
    name: str


class Strict(RequestBody):
    name: str


class Reopened(RequestBody):
    model_config = ConfigDict(extra="ignore")
    name: str


class Holder(RequestBody):
    inner: list[Inner] | None = None


def test_the_walk_finds_every_way_to_accept_extra_keys() -> None:
    """Each shape below accepts an unknown key, and the walk names each of them."""
    app = FastAPI()

    @app.post("/plain")
    def plain(body: Inner) -> None: ...

    @app.post("/reopened")
    def reopened(body: Reopened) -> None: ...

    @app.post("/nested")
    def nested(body: Holder) -> None: ...

    @app.post("/embedded")
    def embedded(body: Annotated[Strict, Body(embed=True)]) -> None: ...

    @app.post("/two")
    def two(first: Strict, second: Annotated[int, Body()]) -> None: ...

    @app.post("/mapping")
    def mapping(body: dict[str, str]) -> None: ...

    def strict_dependency(body: Strict) -> Strict:
        return body

    @app.post("/dependency")
    def dependency(body: Annotated[Strict, Depends(strict_dependency)]) -> None: ...

    @app.post("/no-body")
    def no_body() -> None: ...

    assert problems(app) == [
        "POST /plain: not a RequestBody",
        "POST /reopened: Reopened allows extra keys",
        "POST /nested: Inner allows extra keys",
        "POST /embedded: not a RequestBody",
        "POST /two: not a RequestBody",
        "POST /mapping: not a RequestBody",
    ]


# The status each body route answers a sample with; a route not named here answers 200.
CREATED: dict[type[BaseModel], int] = {NewLabel: 201}


@dataclass(frozen=True)
class BodyRoute:
    method: str
    path: str
    sample: dict[str, Any]
    status: int


@pytest.fixture(params=list(SAMPLES), ids=[model.__name__ for model in SAMPLES])
def body_route(client: TestClient, app: FastAPI, request: pytest.FixtureRequest) -> BodyRoute:
    model = request.param
    paths = [where for where, found in body_routes(app).items() if found is model]
    assert len(paths) == 1
    method, path = paths[0].split(" ", 1)
    if "{label_id}" in path:
        # A route on one label needs a label to exist, made through the API.
        made = client.post("/api/labels", json={"kind": "tag", "name": "Sample"})
        assert made.status_code == 201
        path = path.replace("{label_id}", made.json()["id"])
    assert "{" not in path, path
    return BodyRoute(method, path, SAMPLES[model], CREATED.get(model, 200))


def test_a_known_payload_still_passes(client: TestClient, body_route: BodyRoute) -> None:
    response = client.request(body_route.method, body_route.path, json=body_route.sample)
    assert response.status_code == body_route.status, response.text


@pytest.mark.parametrize(
    "extra",
    [
        {CANARY: CANARY},
        {CANARY: None},
        {"lable": CANARY},
        {"Query": CANARY},
        {"": CANARY},
        {CANARY: {"query": CANARY}},
        # The name of the route's body parameter, which is not a key of the body.
        {"body": CANARY},
    ],
)
def test_an_unknown_key_is_refused_without_its_name_or_value(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    body_route: BodyRoute,
    extra: dict[str, Any],
) -> None:
    response = client.request(
        body_route.method, body_route.path, json={**body_route.sample, **extra}
    )
    assert_private(response, caplog)
    assert response.json() == {"detail": [{"loc": ["body"], **INVALID}]}
    for key in extra:
        if key and key != "body":
            assert key not in response.text
            assert key.lower() not in response.text.lower()


def test_an_unknown_key_beside_a_bad_value_names_only_the_declared_field(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.post("/api/search", json={"query": "a", "limit": CANARY, "lable": CANARY})
    assert_private(response, caplog)
    assert response.json() == {
        "detail": [{"loc": ["body", "limit"], **INVALID}, {"loc": ["body"], **INVALID}]
    }
    assert "lable" not in response.text


def test_the_schema_says_no_other_keys(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    for model in SAMPLES:
        assert schemas[model.__name__]["additionalProperties"] is False
