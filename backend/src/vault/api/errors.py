"""The answer to a request that fails validation: a 422 in fixed text.

FastAPI's own handler answers with pydantic's errors, which quote the rejected value (``input``)
and often repeat it in the message or ``ctx``. A sort key, a label id, a search query or a header
can be as private as a file name, so this handler answers every ``RequestValidationError`` with
the same shape and nothing from the request: each entry names the place (``query``, ``path``,
``header``, ``cookie`` or ``body``) and, only when the route itself declares it, the parameter or
body field, and says ``missing`` or ``invalid``. Never the value, never pydantic's message, never
a name the client made up (an unknown body key, a dictionary key, a list index). Nothing here is
logged.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

PLACES: frozenset[str] = frozenset({"query", "path", "header", "cookie", "body"})
Kind = Literal["missing", "invalid"]

MESSAGES: dict[Kind, str] = {"missing": "a required value is missing", "invalid": "not valid"}


class ValidationError(BaseModel):
    """One refused part of the request: where, and whether it was missing or not valid."""

    loc: list[str] = Field(title="Location")
    msg: str = Field(title="Message")
    type: Kind = Field(title="Error Type")


class HTTPValidationError(BaseModel):
    detail: list[ValidationError]


def _dependants(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for child in dependant.dependencies:
        yield from _dependants(child)


def _names(field: Any, *, header: bool) -> set[str]:
    names = {field.name, field.alias}
    alias = getattr(field, "validation_alias", None)
    if isinstance(alias, str):
        names.add(alias)
    if header:
        names |= {name.replace("_", "-") for name in list(names)}
    return names


def _body_names(field: Any) -> set[str]:
    names = _names(field, header=False)
    annotation = getattr(field.field_info, "annotation", None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        for name, info in annotation.model_fields.items():
            names.add(name)
            if isinstance(info.alias, str):
                names.add(info.alias)
            if isinstance(info.validation_alias, str):
                names.add(info.validation_alias)
    return names


def declared_names(route: APIRoute) -> dict[str, frozenset[str]]:
    """The names each place may name for ``route``: those its code declares, nothing else."""
    found: dict[str, set[str]] = {place: set() for place in PLACES}
    for dependant in _dependants(route.dependant):
        for field in dependant.query_params:
            found["query"] |= _names(field, header=False)
        for field in dependant.path_params:
            found["path"] |= _names(field, header=False)
        for field in dependant.header_params:
            found["header"] |= _names(field, header=True)
        for field in dependant.cookie_params:
            found["cookie"] |= _names(field, header=False)
        for field in dependant.body_params:
            found["body"] |= _body_names(field)
    return {place: frozenset(names) for place, names in found.items()}


def _entry(loc: Sequence[Any], kind: str, names: dict[str, frozenset[str]]) -> ValidationError:
    place = loc[0] if loc and isinstance(loc[0], str) and loc[0] in PLACES else None
    where: list[str] = []
    if place is not None:
        where.append(place)
        # An unknown top-level key is the client's own name even when it spells one the route
        # declares elsewhere (``body``, the parameter's name, is not a key of a body that is one
        # model). An unknown key deeper down still names the declared field that holds it.
        declared = not (kind == "extra_forbidden" and len(loc) == 2)
        if declared and len(loc) > 1 and isinstance(loc[1], str) and loc[1] in names.get(place, ()):
            where.append(loc[1])
    fixed: Kind = "missing" if kind == "missing" else "invalid"
    return ValidationError(loc=where, msg=MESSAGES[fixed], type=fixed)


def fixed_errors(error: RequestValidationError, route: object) -> HTTPValidationError:
    """The fixed-text body for ``error``: one entry per distinct place, in the order found."""
    names = declared_names(route) if isinstance(route, APIRoute) else {}
    entries: list[ValidationError] = []
    for raw in error.errors():
        loc = raw.get("loc", ()) if isinstance(raw, dict) else ()
        kind = raw.get("type") if isinstance(raw, dict) else None
        entry = _entry(loc if isinstance(loc, Sequence) else (), str(kind), names)
        if entry not in entries:
            entries.append(entry)
    return HTTPValidationError(detail=entries)


async def validation_error_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, RequestValidationError):  # pragma: no cover - registered for it
        raise error
    body = fixed_errors(error, request.scope.get("route"))
    return JSONResponse(status_code=422, content=body.model_dump())


def install(app: FastAPI) -> None:
    """Answer every request validation error in fixed text, and document that shape."""
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    openapi = app.openapi

    def documented() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = openapi()
        schemas = schema.get("components", {}).get("schemas", {})
        # FastAPI documents its own error body under these names; describe the one sent instead.
        if "ValidationError" in schemas:
            schemas["ValidationError"] = ValidationError.model_json_schema()
        if "HTTPValidationError" in schemas:
            item = {"$ref": "#/components/schemas/ValidationError"}
            schemas["HTTPValidationError"] = {
                "title": "HTTPValidationError",
                "type": "object",
                "properties": {"detail": {"title": "Detail", "type": "array", "items": item}},
                "required": ["detail"],
            }
        app.openapi_schema = schema
        return schema

    app.openapi = documented  # type: ignore[method-assign]
