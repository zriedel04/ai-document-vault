"""The vault database: WAL mode, per-connection pragmas, and the migrations."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from vault.core.logging import SafeFormatter, configure_logging
from vault.db.engine import BUSY_TIMEOUT_MS, DatabaseError, create_vault_engine
from vault.db.migrate import alembic_config, upgrade_to_latest
from vault.db.schema import metadata


def pragma(db_file: Path, name: str) -> object:
    connection = sqlite3.connect(db_file)
    try:
        return connection.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        connection.close()


def test_migrating_creates_the_database_in_wal_mode(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    assert db_file.is_file()
    # A plain connection that never ran our pragmas sees the mode stored in the file.
    assert str(pragma(db_file, "journal_mode")).lower() == "wal"


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        with engine.connect() as connection:
            versions = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    finally:
        engine.dispose()
    head = ScriptDirectory.from_config(alembic_config(db_file)).get_current_head()
    assert [row[0] for row in versions] == [head]


def test_the_migrations_match_the_table_definitions(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), metadata)
    finally:
        engine.dispose()
    assert differences == []


def test_the_baseline_can_be_undone(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    command.downgrade(alembic_config(db_file), "base")
    engine = create_vault_engine(db_file)
    try:
        assert "app_meta" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_every_connection_gets_the_pragmas(tmp_path: Path) -> None:
    engine = create_vault_engine(tmp_path / "vault.db")
    try:
        for _ in range(2):  # a second connection is configured too, not only the first
            with engine.connect() as one, engine.connect() as two:
                for connection in (one, two):
                    assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
                    assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
                    assert connection.execute(text("PRAGMA synchronous")).scalar() == 1
                    assert (
                        connection.execute(text("PRAGMA busy_timeout")).scalar() == BUSY_TIMEOUT_MS
                    )
    finally:
        engine.dispose()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    engine = create_vault_engine(tmp_path / "vault.db")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
            connection.execute(
                text("CREATE TABLE child (parent_id INTEGER REFERENCES parent (id))")
            )
        with pytest.raises(Exception, match="FOREIGN KEY"), engine.begin() as connection:
            connection.execute(text("INSERT INTO child VALUES (99)"))
    finally:
        engine.dispose()


def test_a_database_that_cannot_use_wal_is_an_error() -> None:
    """An in-memory database reports its journal mode as ``memory``: refuse to run on it."""
    engine = create_vault_engine(Path(":memory:"))
    try:
        with pytest.raises(DatabaseError, match="WAL"), engine.connect():
            pass
    finally:
        engine.dispose()


def test_migrating_leaves_the_applications_logging_alone(tmp_path: Path) -> None:
    configure_logging("INFO")
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    upgrade_to_latest(tmp_path / "vault.db")
    assert root.handlers == handlers
    assert root.level == level
    assert isinstance(root.handlers[0].formatter, SafeFormatter)


def test_the_app_builds_the_vault_and_database_at_start_up(app: FastAPI, vault_dir: Path) -> None:
    assert (vault_dir / "vault.db").is_file()
    assert (vault_dir / "blobs").is_dir()
    assert (vault_dir / "local-token").is_file()
    assert app.state.engine is not None


def test_the_engine_is_released_when_the_app_stops(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposed: list[bool] = []
    real = app.state.engine.dispose

    def spy() -> None:
        disposed.append(True)
        real()

    monkeypatch.setattr(app.state.engine, "dispose", spy)
    with TestClient(app, base_url="http://localhost"):
        assert disposed == []
    assert disposed == [True]
