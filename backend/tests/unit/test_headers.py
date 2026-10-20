"""Every response carries the security headers, whatever produced it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from vault.core.config import Settings
from vault.core.headers import CONTENT_SECURITY_POLICY, SECURITY_HEADERS, SecurityHeadersMiddleware
from vault.main import create_app

LOCAL = "http://localhost"


def assert_secured(response: object) -> None:
    headers = response.headers  # type: ignore[attr-defined]
    for name, value in SECURITY_HEADERS:
        assert headers.get_list(name) == [value], name


@pytest.fixture
def served(tmp_path: Path) -> FastAPI:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")
    return create_app(Settings.model_validate({"frontend": {"dist": str(dist)}}))


def test_the_policy_denies_by_default_and_allows_scripts_only_from_this_origin() -> None:
    directives = dict(part.split(" ", 1) for part in CONTENT_SECURITY_POLICY.split("; "))
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"  # no 'unsafe-inline', no 'unsafe-eval', no host
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'"
    # Nothing an injected style or image could use to send data to another host.
    for outward in ("connect-src", "img-src", "font-src", "form-action"):
        assert directives[outward] == "'self'", outward


def test_workers_load_only_from_this_origin() -> None:
    # The PDF viewer runs PDF.js in a worker. A worker's source is worker-src, else child-src,
    # else script-src (CSP Level 3), so it is this origin's own files and nothing else.
    directives = dict(part.split(" ", 1) for part in CONTENT_SECURITY_POLICY.split("; "))
    effective = directives.get("worker-src", directives.get("child-src", directives["script-src"]))
    assert effective == "'self'"
    # No eval, WebAssembly, blob: or wildcard source anywhere, and inline code only for styles
    # (Mantine's, see the module docstring).
    for name, value in directives.items():
        sources = value.split()
        for loosening in ("'unsafe-eval'", "'wasm-unsafe-eval'", "blob:", "data:", "*"):
            assert loosening not in sources, name
        if name != "style-src":
            assert "'unsafe-inline'" not in sources, name


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", "/", {}, 200),  # the app
        ("GET", "/documents", {}, 200),  # a client-side route
        ("GET", "/assets/app.js", {}, 200),  # a built file
        ("GET", "/api/health", {}, 200),  # the open API endpoint
        ("GET", "/api/session", "token", 200),  # an API endpoint with the token
        ("GET", "/api/session", {}, 401),  # refused: no token
        ("GET", "/api/nothing-here", "token", 404),
        ("DELETE", "/api/health", "token", 405),
        ("GET", "/", {"Host": "attacker.example"}, 403),  # refused: DNS rebinding
        ("POST", "/api/health", {"Origin": "http://attacker.example"}, 403),  # refused: origin
    ],
)
def test_every_kind_of_response_carries_the_headers(
    served: FastAPI, method: str, path: str, headers: dict[str, str] | str, status: int
) -> None:
    client = TestClient(served, base_url=LOCAL)
    if headers == "token":
        headers = {"Authorization": f"Bearer {served.state.token}", "Origin": LOCAL}
    assert isinstance(headers, dict)
    response = client.request(method, path, headers=headers)
    assert response.status_code == status
    assert_secured(response)


def test_a_header_set_further_in_is_replaced_not_repeated() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        response = PlainTextResponse(
            "x", headers={"Content-Security-Policy": "default-src *", "X-Frame-Options": "ALLOW"}
        )
        await response(scope, receive, send)

    assert_secured(TestClient(SecurityHeadersMiddleware(app)).get("/"))


def test_an_unhandled_error_is_answered_with_the_headers_and_still_raised() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    secured = SecurityHeadersMiddleware(app)
    with pytest.raises(RuntimeError):
        TestClient(secured).get("/")
    response = TestClient(secured, raise_server_exceptions=False).get("/")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert_secured(response)


def test_an_unhandled_error_in_the_real_app_is_answered_with_the_headers() -> None:
    # Without the frontend, whose catch-all routes would answer a route added after them.
    served = create_app(Settings(), serve_frontend=False)

    @served.get("/api/explode")
    def explode() -> None:
        raise RuntimeError("boom")

    client = TestClient(
        served,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {served.state.token}"},
        raise_server_exceptions=False,
    )
    response = client.get("/api/explode")
    assert response.status_code == 500
    assert_secured(response)


def test_a_non_http_scope_passes_through_untouched() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def nothing() -> dict[str, str]:
        return {}

    async def ignore(_message: object) -> None:
        return None

    asyncio.run(SecurityHeadersMiddleware(app)({"type": "lifespan"}, nothing, ignore))
    assert seen == ["lifespan"]
