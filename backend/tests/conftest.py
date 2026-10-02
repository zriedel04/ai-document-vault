"""Shared fixtures. Every test uses a temporary vault; nothing touches a real one."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vault.core.config import Settings
from vault.main import create_app
from vault.storage import acl

LOCAL = "http://localhost"


@pytest.fixture(autouse=True)
def _skip_powershell(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting PowerShell for every test vault would slow the Windows job for no gain.

    ``tests/unit/test_acl.py`` covers the Windows access-list step, including a real run.
    """
    if request.module.__name__.rsplit(".", 1)[-1] != "test_acl":
        monkeypatch.setattr(acl, "IS_WINDOWS", False)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def app(vault_dir: Path) -> FastAPI:
    return create_app(Settings.model_validate({"vault": {"path": str(vault_dir)}}))


@pytest.fixture
def token(app: FastAPI) -> str:
    return str(app.state.token)


@pytest.fixture
def anonymous(app: FastAPI) -> TestClient:
    """A client with the right Host and no token."""
    return TestClient(app, base_url=LOCAL)


@pytest.fixture
def client(app: FastAPI, token: str) -> TestClient:
    """A client with the right Host, the token, and the Origin a same-site page would send."""
    return TestClient(
        app, base_url=LOCAL, headers={"Authorization": f"Bearer {token}", "Origin": LOCAL}
    )
