"""Apply the Alembic migrations to ``vault.db``."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(db_file: Path) -> Config:
    """An Alembic configuration built in code, so there is no ``alembic.ini``.

    A stock ``env.py`` calls ``logging.config.fileConfig``, which would replace the application's
    safe logging. Ours does not, and a test checks that.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["db_file"] = db_file
    return config


def upgrade_to_latest(db_file: Path) -> None:
    """Create ``db_file`` if needed and bring it to the latest schema. Safe to repeat."""
    command.upgrade(alembic_config(db_file), "head")
