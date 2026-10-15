"""Response headers that limit what a page from this server may do in the browser.

The access token lives in the page's ``sessionStorage`` (ADR 0016), so any script that runs in this
origin can read it. React renders document text as text, and these headers are the second line:
if HTML ever got into the page, the browser still refuses to run or load what it brings.

:class:`SecurityHeadersMiddleware` adds them to every HTTP response, the API's JSON, the built
frontend and its files, and the refusals of ``LocalSecurityMiddleware`` and the error page alike:

``Content-Security-Policy`` (:data:`CONTENT_SECURITY_POLICY`)
    Everything is denied unless listed. Scripts come only from this origin, as files: no inline
    script, no ``eval``, no other host. Requests (``fetch``), images and fonts go only to this
    origin, which also means that injected markup or CSS has nowhere outside to send what it reads.
    Styles may be inline because Mantine writes its theme into ``<style>`` elements while the page
    runs; with every outward channel closed, an injected style can change how the page looks but
    cannot carry data away. No page may frame this one, no ``<base>`` may redirect its relative
    links, and forms submit only to this origin.
``X-Content-Type-Options: nosniff``
    The browser uses the content type it is given and never guesses that a file is a script.
``Referrer-Policy: no-referrer``
    Following a link never tells the other site which page it came from.
``X-Frame-Options: DENY``
    The framing rule again, for browsers that predate ``frame-ancestors``.
``Cross-Origin-Opener-Policy: same-origin``
    A page of another site that opens this one gets no handle on its window.
``Cross-Origin-Resource-Policy: same-origin``
    Another site cannot embed a response of this server (a ``<script>`` or ``<img>`` pointing at
    ``/api/...``).

FastAPI's ``/api/docs`` page loads Swagger UI from a CDN with an inline script, which this policy
blocks. That page already needs the bearer token, which a browser address cannot carry, so it was
never usable from a browser; read the schema at ``/api/openapi.json`` instead.
"""

from __future__ import annotations

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
SECURITY_HEADERS: Final = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware that sets :data:`SECURITY_HEADERS` on every HTTP response.

    Add it last, so it is the outermost of the application's middleware and also covers the
    responses that ``LocalSecurityMiddleware`` sends itself. An exception that escapes the
    application before a response has started is answered here with a plain 500 that carries the
    headers, because Starlette's own error page is sent outside every added middleware; the
    exception is then raised again so that it is still reported.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers[name] = value  # replaces a value set further in, never duplicates it
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if not started:
                await send_with_headers(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send({"type": "http.response.body", "body": b"Internal Server Error"})
            raise
