"""Alembic environment. Migrations run in process, against the engine the application uses."""

from __future__ import annotations

from alembic import context

from vault.db.engine import create_vault_engine
from vault.db.schema import include_name, metadata

config = context.config
db_file = config.attributes["db_file"]

# No fileConfig(): it would replace the application's logging (see vault.db.migrate).
engine = create_vault_engine(db_file)
try:
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=metadata, include_name=include_name
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
