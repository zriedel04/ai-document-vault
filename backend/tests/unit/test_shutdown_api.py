"""``POST /api/shutdown``: what ``vault.ps1 stop`` asks the server it started (plan section 4)."""

from __future__ import annotations

import http.client
import logging
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vault import __main__ as entry
from vault.core.config import Settings
from vault.core.logging import SafeFormatter
from vault.main import WORKER_STOP_SECONDS, create_app

from ..conftest import LOCAL

URL = "/api/shutdown"
LOOPBACK = ("127.0.0.1", 50000)


class Calls:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def calls(app: FastAPI) -> Calls:
    recorder = Calls()
    app.state.request_shutdown = recorder
    return recorder


def local_client(app: FastAPI, token: str, client: tuple[str, int] = LOOPBACK) -> TestClient:
    return TestClient(
        app,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {token}", "Origin": LOCAL},
        client=client,
    )


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_a_local_request_with_the_token_stops_the_server_after_the_answer(
    app: FastAPI, token: str, calls: Calls, address: str
) -> None:
    response = local_client(app, token, (address, 50000)).post(URL)
    assert response.status_code == 202
    assert response.json() == {"stopping": True}
    assert calls.count == 1


def test_the_request_needs_the_token(app: FastAPI, calls: Calls) -> None:
    client = TestClient(app, base_url=LOCAL, headers={"Origin": LOCAL}, client=LOOPBACK)
    assert client.post(URL).status_code == 401
    wrong = local_client(app, "x" * 43)
    assert wrong.post(URL).status_code == 401
    assert calls.count == 0


@pytest.mark.parametrize("origin", [None, "http://evil.example", "http://localhost:9999"])
def test_the_request_needs_the_matching_origin(
    app: FastAPI, token: str, calls: Calls, origin: str | None
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    if origin is not None:
        headers["Origin"] = origin
    client = TestClient(app, base_url=LOCAL, headers=headers, client=LOOPBACK)
    assert client.post(URL).status_code == 403
    assert calls.count == 0


def test_the_request_needs_a_local_host_header(app: FastAPI, token: str, calls: Calls) -> None:
    client = TestClient(
        app,
        base_url="http://vault.example",
        headers={"Authorization": f"Bearer {token}", "Origin": "http://vault.example"},
        client=LOOPBACK,
    )
    assert client.post(URL).status_code == 403
    assert calls.count == 0


@pytest.mark.parametrize("address", ["192.168.1.20", "10.0.0.5", "127.0.0.2", "testclient", "::2"])
def test_a_request_from_another_address_is_refused_even_with_the_token(
    app: FastAPI, token: str, calls: Calls, address: str
) -> None:
    response = local_client(app, token, (address, 50000)).post(URL)
    assert response.status_code == 403
    assert response.json() == {"detail": "request refused"}
    assert calls.count == 0


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_only_post_stops_the_server(app: FastAPI, token: str, calls: Calls, method: str) -> None:
    assert local_client(app, token).request(method, URL).status_code == 405
    assert calls.count == 0


def test_a_server_that_cannot_stop_itself_says_so(app: FastAPI, token: str) -> None:
    # create_app alone (tests, the schema export) has no uvicorn server to stop.
    assert app.state.request_shutdown is None
    response = local_client(app, token).post(URL)
    assert response.status_code == 503
    assert response.json() == {"detail": "this server cannot stop itself"}


def test_the_launcher_waits_longer_than_a_clean_shutdown_may_take() -> None:
    """``vault.ps1 stop`` ends the process when it has not exited by its deadline. Uvicorn lets
    open requests finish for ``SHUTDOWN_GRACE_SECONDS``, then the pool gives running jobs
    ``WORKER_STOP_SECONDS``; a shorter deadline would kill every shutdown that has a job running."""
    launcher = Path(__file__).resolve().parents[3] / "vault.ps1"
    found = re.search(r"^\$script:StopSeconds = (\d+)\r?$", launcher.read_text("utf-8"), re.M)
    assert found is not None
    assert int(found.group(1)) >= entry.SHUTDOWN_GRACE_SECONDS + WORKER_STOP_SECONDS + 10


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(port: int, method: str, path: str, headers: dict[str, str] | None = None) -> int | None:
    """The status of one request to the local server, or None if nothing answered."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        return connection.getresponse().status
    except OSError:
        return None
    finally:
        connection.close()


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_the_entry_point_stops_cleanly_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real ``python -m vault`` (in a thread): the request is answered, uvicorn shuts down, the
    lifespan stops the worker pool and ``main`` returns."""
    port = free_port()
    vault_dir = tmp_path / "vault-José"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AIVAULT_CONFIG", raising=False)
    monkeypatch.setenv("AIVAULT_SERVER__PORT", str(port))
    monkeypatch.setenv("AIVAULT_VAULT__PATH", str(vault_dir))
    collector = Collect()
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            entry.main()
        except BaseException as error:  # reported by the assertion below
            failures.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while request(port, "GET", "/api/health") != 200:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.05)
        logging.getLogger().addHandler(collector)
        token = (vault_dir / "local-token").read_text("ascii").strip()
        headers = {"Authorization": f"Bearer {token}", "Origin": f"http://127.0.0.1:{port}"}
        assert request(port, "POST", URL, headers) == 202
        thread.join(timeout=30)
        assert not thread.is_alive(), "the server did not exit"
        assert failures == []
        assert request(port, "GET", "/api/health") is None
    finally:
        logging.getLogger().removeHandler(collector)
        logging.getLogger().handlers.clear()
    events = [SafeFormatter().format(record) for record in collector.records]
    text = "\n".join(events)
    assert "server.shutdown_requested" in text
    assert "workers.stopped" in text
    assert token not in text


def test_the_entry_point_gives_uvicorn_the_grace_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AIVAULT_CONFIG", raising=False)
    seen: list[Any] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: seen.append(self))
    entry.main()
    logging.getLogger().handlers.clear()
    (server,) = seen
    assert server.config.timeout_graceful_shutdown == entry.SHUTDOWN_GRACE_SECONDS
    app = server.config.app
    assert app.state.request_shutdown is not None
    assert server.should_exit is False
    app.state.request_shutdown()
    assert server.should_exit is True


def test_the_app_without_the_entry_point_has_no_way_to_stop() -> None:
    assert create_app(Settings()).state.request_shutdown is None
