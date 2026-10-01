import logging
from typing import Any

import pytest
import uvicorn

from vault import __main__ as entry
from vault.core.config import Settings
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
    assert calls == [{"host": "127.0.0.1", "port": 9200, "log_config": None}]
    logging.getLogger().handlers.clear()
