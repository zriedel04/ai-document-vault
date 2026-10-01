import logging
import socket
import threading
import time
import urllib.request
from typing import Any

import pytest
import uvicorn

from vault import __main__ as entry
from vault.core.config import Settings
from vault.core.logging import SafeFormatter
from vault.main import create_app


def test_app_keeps_its_settings() -> None:
    settings = Settings.model_validate({"server": {"port": 9100}})
    assert create_app(settings).state.settings is settings


def test_create_app_loads_settings_when_none_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AIVAULT_CONFIG", raising=False)
    assert create_app().state.settings.server.port == 8765


def test_entry_point_runs_uvicorn_on_the_configured_loopback_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AIVAULT_CONFIG", raising=False)
    monkeypatch.setenv("AIVAULT_SERVER__PORT", "9200")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    entry.main()
    assert calls == [{"host": "127.0.0.1", "port": 9200, "log_config": None, "access_log": False}]
    logging.getLogger().handlers.clear()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_request_urls_never_reach_the_logs_of_a_real_server() -> None:
    """The access log would write the path and query of every request: it must be off."""
    app = create_app(Settings())  # installs our root handler, replacing any other
    root = logging.getLogger()
    collector = Collect()
    root.addHandler(collector)
    root.setLevel(logging.DEBUG)
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        url = f"http://127.0.0.1:{port}/api/health?query=Diagnosis"
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        root.removeHandler(collector)
    names = {record.name for record in collector.records}
    assert "uvicorn.error" in names  # the server did log, and we were listening
    assert "uvicorn.access" not in names
    text = "\n".join(SafeFormatter().format(record) for record in collector.records)
    assert "Diagnosis" not in text
    assert "/api/health" not in text
