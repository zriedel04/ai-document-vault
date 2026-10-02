"""Protections for a server that only this person's browser should talk to.

Binding to 127.0.0.1 keeps other computers out, but not other websites (a page you visit can make
your browser call ``http://127.0.0.1:8765``) and not other accounts on the same PC. So every request
passes through :class:`LocalSecurityMiddleware`, which is deny by default:

1. ``Host`` must be ``localhost``, ``127.0.0.1`` or ``[::1]``, with an optional port. This stops DNS
   rebinding, where an attacker's domain is pointed at 127.0.0.1 and the browser sends that domain.
2. A request that can change something (any method but GET, HEAD and OPTIONS) must carry an
   ``Origin`` that is exactly the origin the ``Host`` header names, so another site cannot send it.
3. Every ``/api`` path except the public one below needs the per-install token as
   ``Authorization: Bearer <token>``, and only that. There is deliberately no cookie: a browser
   sends cookies to every port on the same host, so another local program (for example a dev
   server on ``localhost:3000``) would receive the token. The page keeps the token in its own
   origin's storage and sends it explicitly, which no other port can read.

There is no CORS configuration, on purpose: no other origin is ever allowed. A rejection is logged
as its reason only (never a header value), and the response repeats nothing the client sent.

Every route goes through :func:`current_user`, which today returns the one local user. Adding real
login later changes that function, not the endpoints.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from vault.core.logging import log_event

logger = logging.getLogger(__name__)

API_PREFIX: Final = "/api"
# The only API request that does not need the token: liveness.
PUBLIC_API: Final = frozenset({("GET", "/api/health")})
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
# Always use ``fullmatch`` with these (``$`` also matches before a trailing newline).
HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?", re.IGNORECASE)
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
TOKEN_FILE_MODE: Final = 0o600


class Reason(Enum):
    HOST = "host"
    ORIGIN = "origin"
    AUTH = "auth"
    PROTOCOL = "protocol"


@dataclass(frozen=True)
class LocalUser:
    """The single person who uses this install."""

    id: str = "local"
    name: str = "Local user"


LOCAL_USER: Final = LocalUser()


def current_user() -> LocalUser:
    """FastAPI dependency for every endpoint: who is making this request."""
    return LOCAL_USER


def new_token() -> str:
    return secrets.token_urlsafe(32)


def load_or_create_token(token_file: Path) -> str:
    """Return the install's token, creating the file (owner-only where the OS supports it) once.

    A token file that is not a well-formed token is an error rather than something to trust or
    silently replace: delete it to have a new one made.
    """
    try:
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, TOKEN_FILE_MODE)
    except FileExistsError:
        token = token_file.read_text(encoding="ascii", errors="replace").strip()
        if not TOKEN.fullmatch(token):
            msg = "the token file is not a valid token; delete it to create a new one"
            raise ValueError(msg) from None
        return token
    token = new_token()
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
    return token


def token_matches(expected: str, supplied: str) -> bool:
    """Constant-time comparison that also accepts non-ASCII input (it simply does not match)."""
    return secrets.compare_digest(expected.encode(), supplied.encode())


def supplied_token(headers: Headers) -> str | None:
    """The token the request carries as a bearer header, or None. Cookies are never read."""
    authorization = headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


def _host_is_local(headers: Headers) -> str | None:
    """The ``Host`` value if it is a single, local one, else None."""
    values = headers.getlist("host")
    if len(values) == 1 and HOST.fullmatch(values[0]):
        return values[0]
    return None


def _origin_matches(headers: Headers, host: str) -> bool:
    origins = headers.getlist("origin")
    return len(origins) == 1 and origins[0].lower() == f"http://{host}".lower()


def _is_api(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


class LocalSecurityMiddleware:
    """Pure ASGI middleware applying the rules in the module docstring."""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # WebSockets are not used; deny anything unexpected
            await self._reject_other(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host = _host_is_local(headers)
        if host is None:
            await self._reject(scope, receive, send, 403, Reason.HOST)
            return
        method = scope["method"]
        if method not in SAFE_METHODS and not _origin_matches(headers, host):
            await self._reject(scope, receive, send, 403, Reason.ORIGIN)
            return
        path = scope["path"]
        if _is_api(path) and (method, path) not in PUBLIC_API:
            supplied = supplied_token(headers)
            if supplied is None or not token_matches(self.token, supplied):
                await self._reject(scope, receive, send, 401, Reason.AUTH)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int, why: Reason) -> None:
        log_event(logger, "security.request_rejected", level=logging.WARNING, reason=why)
        response = JSONResponse({"detail": "request refused"}, status_code=status)
        await response(scope, receive, send)

    @staticmethod
    async def _reject_other(scope: Scope, receive: Receive, send: Send) -> None:
        log_event(
            logger, "security.request_rejected", level=logging.WARNING, reason=Reason.PROTOCOL
        )
        if scope["type"] == "websocket":
            await receive()  # the connect message
            await send({"type": "websocket.close", "code": 1008})
