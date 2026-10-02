#!/usr/bin/env python3
"""Check that a running server serves the built frontend and the API from one origin.

CI builds the frontend, starts the backend and runs this against it. Standard library only.
Usage: python scripts/smoke_test_server.py [BASE_URL]    # default http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence

TIMEOUT_SECONDS = 30


def fetch(url: str) -> tuple[int, str, str]:
    """Status, content type and body of a GET; HTTP errors are returned, not raised."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            kind = response.headers.get("content-type", "")
            return response.status, kind, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("content-type", ""), error.read().decode()


def wait_for_server(base: str) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        try:
            fetch(f"{base}/api/health")
            return
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"server did not start at {base}") from None
            time.sleep(0.5)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    base = (args[0] if args else "http://127.0.0.1:8765").rstrip("/")
    if not base.startswith("http://"):
        print("only http:// URLs are checked (the server is local-only)", file=sys.stderr)
        return 2
    wait_for_server(base)

    failures = []

    status, _, body = fetch(f"{base}/api/health")
    if status != 200 or json.loads(body).get("status") != "ok":
        failures.append(f"/api/health: expected status ok, got {status} {body[:80]!r}")

    for path in ("/", "/documents", "/settings"):
        status, kind, body = fetch(f"{base}{path}")
        if status != 200 or "text/html" not in kind or 'id="root"' not in body:
            failures.append(f"{path}: expected the app's index.html, got {status} {kind}")

    # Every /api path but the open ones needs the token, so without one an unknown path is 401
    # (the server does not say which paths exist), and so is a real one.
    for path in ("/api/does-not-exist", "/api/session"):
        status, _, _ = fetch(f"{base}{path}")
        if status != 401:
            failures.append(f"{path}: expected 401 without a token, got {status}")
    status, _, _ = fetch(f"{base}/missing.js")
    if status != 404:
        failures.append(f"/missing.js: expected 404, got {status}")

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    if not failures:
        print(f"smoke test passed against {base}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
