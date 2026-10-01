"""The request protections: Host, Origin, token, and the session that carries the token."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vault.core.logging import SafeFormatter
from vault.core.security import (
    COOKIE_NAME,
    HOST,
    LOCAL_USER,
    LocalSecurityMiddleware,
    current_user,
    load_or_create_token,
    token_matches,
)

Message = MutableMapping[str, Any]
TOKEN = "t" * 43  # a well-formed token for the bare-ASGI tests


async def inner_app(scope: Message, receive: Any, send: Any) -> None:
    """What the middleware protects: it answers 200 to anything that reaches it."""
    if scope["type"] == "lifespan":
        await receive()
        await send({"type": "lifespan.startup.complete"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"reached"})


async def call(
    headers: list[tuple[bytes, bytes]],
    *,
    method: str = "GET",
    path: str = "/api/health",
    scope_type: str = "http",
) -> tuple[int, bytes, list[Message]]:
    """Send one hand-built request through the middleware: full control of every header byte."""
    middleware = LocalSecurityMiddleware(inner_app, token=TOKEN)
    scope: Message = {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
    }
    sent: list[Message] = []
    pending = [{"type": "websocket.connect"}] if scope_type == "websocket" else []

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    status = next((int(m["status"]) for m in sent if m["type"] == "http.response.start"), 0)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, sent


def host(value: str) -> list[tuple[bytes, bytes]]:
    return [(b"host", value.encode())]


def status_of(
    headers: list[tuple[bytes, bytes]], *, method: str = "GET", path: str = "/api/health"
) -> int:
    import asyncio

    return asyncio.run(call(headers, method=method, path=path))[0]


# --- Host -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["localhost", "localhost:8765", "LOCALHOST:80", "127.0.0.1", "127.0.0.1:1", "[::1]:8765"],
)
def test_local_hosts_are_accepted(value: str) -> None:
    assert status_of(host(value)) == 200


@pytest.mark.parametrize(
    "value",
    [
        "evil.example",
        "evil.example:8765",
        "localhost.evil.example",
        "127.0.0.1.evil.example",
        "evil.example/localhost",
        "localhost@evil.example",
        "localhost:",
        "localhost:123456",
        "localhost:80a",
        "localhost.",
        "localhost\n",
        "localhost\r",
        "localhost ",
        " localhost",
        "localhost\x00",
        "127.0.0.2",
        "0.0.0.0",  # noqa: S104
        "[::1",
        "::1",
        "",
    ],
)
def test_other_hosts_are_refused(value: str) -> None:
    assert status_of(host(value)) == 403


def test_a_missing_or_repeated_host_header_is_refused() -> None:
    assert status_of([]) == 403
    assert status_of(host("localhost") + host("localhost")) == 403
    assert status_of(host("localhost") + host("evil.example")) == 403


def test_the_host_pattern_is_matched_as_a_whole() -> None:
    assert HOST.fullmatch("localhost")
    assert not HOST.fullmatch("localhost\n")


def test_a_refusal_repeats_nothing_the_client_sent_and_logs_only_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    caplog.set_level(logging.DEBUG)
    status, body, _ = asyncio.run(call(host("patient-diagnosis.example")))
    assert status == 403
    assert b"patient-diagnosis" not in body
    lines = [SafeFormatter().format(record) for record in caplog.records]
    assert any("security.request_rejected" in line and "reason=host" in line for line in lines)
    assert "patient-diagnosis" not in "\n".join(lines)


# --- Origin ---------------------------------------------------------------------------------

CHANGING = ["POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT", "FOO"]


def with_token(extra: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    return [*extra, (b"authorization", f"Bearer {TOKEN}".encode())]


@pytest.mark.parametrize("method", CHANGING)
def test_a_changing_request_needs_a_same_origin_header(method: str) -> None:
    base = host("localhost:8765")
    path = "/api/session"
    assert status_of(with_token(base), method=method, path=path) == 403  # no Origin at all
    same = [*base, (b"origin", b"http://localhost:8765")]
    assert status_of(with_token(same), method=method, path=path) == 200


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://evil.example",
        "https://localhost:8765",
        "http://localhost:9999",
        "http://localhost",
        "http://127.0.0.1:8765",
        "http://localhost:8765/",
        "http://localhost:8765\n",
        "http://localhost:8765 ",
        "http://localhost:8765.evil.example",
        "http://evil.example@localhost:8765",
        "",
    ],
)
def test_a_changing_request_from_another_origin_is_refused(origin: str) -> None:
    headers = [*host("localhost:8765"), (b"origin", origin.encode())]
    assert status_of(with_token(headers), method="POST", path="/api/session") == 403


def test_origin_is_compared_without_regard_to_case() -> None:
    headers = [*host("LocalHost:8765"), (b"origin", b"http://localhost:8765")]
    assert status_of(with_token(headers), method="POST", path="/api/session") == 200


def test_two_origin_headers_are_refused() -> None:
    headers = [
        *host("localhost:8765"),
        (b"origin", b"http://localhost:8765"),
        (b"origin", b"http://localhost:8765"),
    ]
    assert status_of(with_token(headers), method="POST", path="/api/session") == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_reading_does_not_need_an_origin(method: str) -> None:
    assert status_of(with_token(host("localhost")), method=method, path="/api/x") == 200


def test_the_host_is_checked_before_the_origin() -> None:
    headers = [*host("evil.example"), (b"origin", b"http://evil.example")]
    assert status_of(with_token(headers), method="POST", path="/api/session") == 403


# --- Token ----------------------------------------------------------------------------------


def test_the_api_needs_the_token() -> None:
    base = host("localhost")
    assert status_of(base, path="/api/documents") == 401
    assert status_of(base, path="/api/") == 401
    assert status_of(base, path="/api") == 401
    assert status_of(base, path="/api/openapi.json") == 401
    assert status_of(base, method="HEAD", path="/api/health") == 401  # only GET is open
    assert status_of(base, method="OPTIONS", path="/api/documents") == 401


@pytest.mark.parametrize(
    "authorization",
    [
        "",
        "Bearer",
        "Bearer ",
        "Bearer wrong",
        f"Bearer {TOKEN[:-1]}",
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN}\n",
        f"Bearer  {TOKEN}",
        f"Basic {TOKEN}",
        TOKEN,
        "Bearer Ünïcödé-tøken",
    ],
)
def test_a_wrong_or_malformed_token_is_refused_not_an_error(authorization: str) -> None:
    headers = [*host("localhost"), (b"authorization", authorization.encode())]
    assert status_of(headers, path="/api/documents") == 401


def test_the_token_is_accepted_as_a_bearer_header_or_as_the_cookie() -> None:
    base = host("localhost")
    bearer = [*base, (b"authorization", f"Bearer {TOKEN}".encode())]
    assert status_of(bearer, path="/api/documents") == 200
    lower = [*base, (b"authorization", f"bearer {TOKEN}".encode())]
    assert status_of(lower, path="/api/documents") == 200
    cookie = [*base, (b"cookie", f"other=1; {COOKIE_NAME}={TOKEN}".encode())]
    assert status_of(cookie, path="/api/documents") == 200


def test_the_token_is_not_accepted_in_the_query_string_or_a_differently_named_cookie() -> None:
    import asyncio

    base = host("localhost")
    assert status_of([*base, (b"cookie", f"session={TOKEN}".encode())], path="/api/x") == 401
    middleware = LocalSecurityMiddleware(inner_app, token=TOKEN)
    scope: Message = {
        "type": "http",
        "method": "GET",
        "path": "/api/x",
        "headers": base,
        "query_string": f"token={TOKEN}".encode(),
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    assert sent[0]["status"] == 401


def test_a_bad_bearer_header_does_not_fall_back_to_a_good_cookie_by_accident() -> None:
    # The bearer header is the one that is checked when it is present.
    headers = [
        *host("localhost"),
        (b"authorization", b"Bearer wrong"),
        (b"cookie", f"{COOKIE_NAME}={TOKEN}".encode()),
    ]
    assert status_of(headers, path="/api/documents") == 401


def test_only_the_open_paths_work_without_a_token() -> None:
    base = host("localhost")
    assert status_of(base, path="/api/health") == 200
    origin = [*base, (b"origin", b"http://localhost")]
    assert status_of(origin, method="POST", path="/api/session") == 200
    assert status_of(origin, method="PUT", path="/api/session") == 401
    assert status_of(base, path="/api/session") == 401  # GET /api/session needs the token


def test_pages_outside_the_api_are_not_behind_the_token() -> None:
    base = host("localhost")
    for path in ("/", "/documents", "/assets/app.js", "/apix", "//api/documents", "/API/documents"):
        assert status_of(base, path=path) == 200, path


def test_a_non_ascii_token_compares_as_a_mismatch() -> None:
    assert not token_matches(TOKEN, "tøken")
    assert token_matches(TOKEN, TOKEN)


def test_websockets_are_refused() -> None:
    import asyncio

    status, _, sent = asyncio.run(call(host("localhost"), scope_type="websocket"))
    assert status == 0
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_the_lifespan_passes_through() -> None:
    import asyncio

    _, _, sent = asyncio.run(call([], scope_type="lifespan"))
    assert sent == [{"type": "lifespan.startup.complete"}]


# --- Through the real application ------------------------------------------------------------


def test_an_unsigned_client_cannot_use_the_api(anonymous: TestClient) -> None:
    assert anonymous.get("/api/session").status_code == 401
    assert anonymous.get("/api/nothing").status_code == 401  # even a missing path says nothing
    assert anonymous.get("/api/openapi.json").status_code == 401
    assert anonymous.get("/api/docs").status_code == 401
    assert anonymous.get("/%61pi/nothing").status_code == 401  # the encoded path is the same path


def test_a_foreign_host_is_refused_even_with_the_token(app: FastAPI, token: str) -> None:
    rebound = TestClient(
        app, base_url="http://evil.example", headers={"Authorization": f"Bearer {token}"}
    )
    assert rebound.get("/api/health").status_code == 403
    assert rebound.get("/api/session").status_code == 403


def test_a_foreign_origin_is_refused_even_with_the_token(app: FastAPI, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}", "Origin": "http://evil.example"}
    page_on_another_site = TestClient(app, base_url="http://localhost", headers=headers)
    assert page_on_another_site.post("/api/session").status_code == 403


def test_the_token_signs_in_with_a_cookie_the_page_cannot_read(
    anonymous: TestClient, token: str
) -> None:
    refused = anonymous.post(
        "/api/session", headers={"Authorization": "Bearer wrong", "Origin": "http://localhost"}
    )
    assert refused.status_code == 401
    assert "set-cookie" not in refused.headers

    response = anonymous.post(
        "/api/session", headers={"Authorization": f"Bearer {token}", "Origin": "http://localhost"}
    )
    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{COOKIE_NAME}={token}".lower())
    assert "httponly" in cookie
    assert "samesite=strict" in cookie

    me = anonymous.get("/api/session", headers={"Cookie": f"{COOKIE_NAME}={token}"})
    assert me.status_code == 200
    assert me.json() == {"id": LOCAL_USER.id, "name": LOCAL_USER.name}


def test_signing_in_needs_the_origin_and_the_token(anonymous: TestClient, token: str) -> None:
    assert (
        anonymous.post("/api/session", headers={"Authorization": f"Bearer {token}"}).status_code
        == 403
    )
    assert anonymous.post("/api/session", headers={"Origin": "http://localhost"}).status_code == 401


def test_current_user_is_the_one_local_user() -> None:
    assert current_user() is LOCAL_USER


# --- The token file -------------------------------------------------------------------------


def test_the_token_is_created_once_and_then_reused(tmp_path: Path) -> None:
    file = tmp_path / "local-token"
    first = load_or_create_token(file)
    assert len(first) >= 32
    assert load_or_create_token(file) == first
    assert file.read_text(encoding="ascii").strip() == first


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses the folder's ACL, not a mode")
def test_the_token_file_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    file = tmp_path / "local-token"
    load_or_create_token(file)
    assert file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n",
        b"short",
        b"x" * 31,
        b"x" * 129,
        b"has space " + b"x" * 40,
        b"a\nb" + b"x" * 40,
        "tøken".encode() * 10,
        b"\xff\xfe" * 30,
    ],
)
def test_a_token_file_that_is_not_a_token_is_an_error(tmp_path: Path, content: bytes) -> None:
    file = tmp_path / "local-token"
    file.write_bytes(content)
    with pytest.raises(ValueError, match="not a valid token"):
        load_or_create_token(file)
    assert file.read_bytes() == content  # and it is left alone


def test_the_same_vault_keeps_the_same_token_across_starts(vault_dir: Path) -> None:
    from vault.core.config import Settings
    from vault.main import create_app

    settings = Settings.model_validate({"vault": {"path": str(vault_dir)}})
    assert create_app(settings).state.token == create_app(settings).state.token


def test_without_a_vault_the_token_is_random_per_process() -> None:
    from vault.core.config import Settings
    from vault.main import create_app

    one = create_app(Settings()).state.token
    two = create_app(Settings()).state.token
    assert one != two
    assert len(one) >= 32


def test_a_bad_token_file_stops_the_start_up(vault_dir: Path) -> None:
    from vault.core.config import Settings
    from vault.main import create_app

    vault_dir.mkdir()
    (vault_dir / "local-token").write_text("not a token", encoding="utf-8")
    with pytest.raises(ValueError, match="not a valid token"):
        create_app(Settings.model_validate({"vault": {"path": str(vault_dir)}}))
    assert not (vault_dir / "vault.db").exists()  # it stopped before touching the database
