"""The SQLite engine for ``vault.db``: SQLAlchemy Core, WAL mode, foreign keys on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import URL, Engine, create_engine, event

BUSY_TIMEOUT_MS = 5000


class DatabaseError(RuntimeError):
    """The database cannot be opened the way the application needs."""


def _configure(dbapi_connection: Any, _record: object) -> None:
    """Run on every new connection: pragmas are per connection, except journal mode."""
    cursor = dbapi_connection.cursor()
    try:
        mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            # Some network and virtual file systems cannot do WAL. Fail rather than run without it.
            msg = "SQLite could not switch the vault database to WAL mode"
            raise DatabaseError(msg)
        cursor.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, much faster than FULL
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def create_vault_engine(db_file: Path) -> Engine:
    """An engine for ``db_file`` (created on first connection). Dispose it when done."""
    # URL.create keeps a Windows path (backslashes, drive letter) intact; a string URL would not.
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(db_file)))
    event.listen(engine, "connect", _configure)
    return engine
