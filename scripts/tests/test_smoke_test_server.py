"""Tests for scripts/smoke_test_server.py: a stand-in server that behaves, and ones that do not."""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import unittest
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import smoke_test_server as smoke  # noqa: E402

INDEX = b'<!doctype html><div id="root"></div>'


class Handler(BaseHTTPRequestHandler):
    """Serves like the real backend, except for the faults switched on in `faults`."""

    faults: frozenset[str] = frozenset()

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self.reply(200, "application/json", b'{"status": "ok", "version": "1"}')
        elif self.path.startswith("/api/"):
            if "api-falls-back-to-app" in self.faults:
                self.reply(200, "text/html", INDEX)
            else:
                self.reply(401, "application/json", b'{"detail": "Unauthorized"}')
        elif "." in self.path:
            self.reply(404, "text/plain", b"missing")
        elif "no-frontend" in self.faults:
            self.reply(404, "application/json", b'{"detail": "Not Found"}')
        else:
            self.reply(200, "text/html; charset=utf-8", INDEX)

    def reply(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def serve(*faults: str) -> Iterator[str]:
    handler = type("Faulty", (Handler,), {"faults": frozenset(faults)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run(url: str) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = smoke.main([url])
    return code, err.getvalue()


class SmokeTest(unittest.TestCase):
    def test_passes_against_a_correct_server(self) -> None:
        with serve() as url:
            self.assertEqual(run(url), (0, ""))

    def test_fails_when_the_frontend_is_not_served(self) -> None:
        with serve("no-frontend") as url:
            code, err = run(url)
        self.assertEqual(code, 1)
        self.assertIn("expected the app's index.html", err)

    def test_fails_when_unknown_api_paths_return_the_app(self) -> None:
        with serve("api-falls-back-to-app") as url:
            code, err = run(url)
        self.assertEqual(code, 1)
        self.assertIn("/api/does-not-exist: expected 401 without a token", err)

    def test_fails_when_an_api_path_does_not_ask_for_the_token(self) -> None:
        with serve("api-falls-back-to-app") as url:
            self.assertIn("/api/session: expected 401 without a token", run(url)[1])

    def test_only_http_urls_are_accepted(self) -> None:
        self.assertEqual(run("https://127.0.0.1:1")[0], 2)
        self.assertEqual(run("file:///etc/passwd")[0], 2)


if __name__ == "__main__":
    unittest.main()
