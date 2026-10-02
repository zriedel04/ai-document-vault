"""Table definitions (SQLAlchemy Core). The migrations in ``migrations/versions`` create them.

Only the baseline lives here. The document tables of ``docs/architecture.md`` section 6 arrive with
the import phase, each with its own migration.
"""

from __future__ import annotations

from sqlalchemy import Column, MetaData, String, Table

# Named constraints, so a later migration can drop one by name on SQLite.
NAMING = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}

metadata = MetaData(naming_convention=NAMING)

# Small facts about this vault (key and value, both text), such as when it was created.
app_meta = Table(
    "app_meta",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
)
