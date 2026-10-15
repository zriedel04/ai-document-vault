"""The document tables: what the database accepts and refuses, and how rows go together."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, Engine, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import alembic_config, upgrade_to_latest
from vault.db.schema import (
    DOCUMENT_STATES,
    IMPORT_METHODS,
    VERSION_ORIGINS,
    blobs,
    document_versions,
    documents,
    import_sources,
)
from vault.ingest.validate import MIME_TYPES

NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)
SHA = "ab" * 32
OTHER_SHA = "cd" * 32


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


def blob_row(**changes: Any) -> dict[str, Any]:
    return {
        "sha256": SHA,
        "size": 10,
        "ext": "pdf",
        "mime": "application/pdf",
        "created_at": NOW,
    } | changes


def document_row(**changes: Any) -> dict[str, Any]:
    return {
        "id": ids.new_ulid(),
        "current_version_id": None,
        "state": "processing",
        "local_only": False,
        "created_at": NOW,
    } | changes


def version_row(document_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": ids.new_ulid(),
        "document_id": document_id,
        "blob_sha256": SHA,
        "version_no": 1,
        "origin": "import",
        "created_at": NOW,
    } | changes


def source_row(document_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": ids.new_ulid(),
        "document_id": document_id,
        "original_path": r"C:\Users\Owner\Downloads\Syllabus.pdf",
        "original_filename": "Syllabus.pdf",
        "folder_segments": json.dumps(["Users", "Owner", "Downloads"]),
        "original_mtime": NOW,
        "import_batch_id": ids.new_ulid(),
        "method": "upload",
        "imported_at": NOW,
    } | changes


def add_document(engine: Engine) -> tuple[str, str]:
    """A document with one version of the blob SHA and one source, as an import writes it."""
    with engine.begin() as connection:
        if connection.execute(select(blobs.c.sha256).where(blobs.c.sha256 == SHA)).first() is None:
            connection.execute(insert(blobs).values(blob_row()))
        document = document_row()
        version = version_row(document["id"])
        connection.execute(insert(documents).values(document))
        connection.execute(insert(document_versions).values(version))
        connection.execute(
            update(documents)
            .where(documents.c.id == document["id"])
            .values(current_version_id=version["id"])
        )
        connection.execute(insert(import_sources).values(source_row(document["id"])))
    return document["id"], version["id"]


def count(engine: Engine, table: Any) -> int:
    with engine.connect() as connection:
        return len(connection.execute(select(table)).all())


# --- ids ---------------------------------------------------------------------------------------


def test_a_ulid_has_26_characters_of_crockfords_alphabet() -> None:
    for _ in range(200):
        value = ids.new_ulid()
        assert len(value) == ids.LENGTH
        assert set(value) <= set(ids.ALPHABET)
        assert ids.is_ulid(value)


def test_ulids_sort_in_time_order() -> None:
    earlier = [ids.new_ulid(now_ms=1_700_000_000_000 + step) for step in range(50)]
    assert earlier == sorted(earlier)
    assert ids.new_ulid(now_ms=0).startswith("0000000000")
    assert ids.new_ulid(now_ms=(1 << 48) - 1).startswith("7ZZZZZZZZZ")


def test_ulids_made_together_differ() -> None:
    assert len({ids.new_ulid(now_ms=1) for _ in range(1000)}) == 1000


@pytest.mark.parametrize("now_ms", [-1, 1 << 48])
def test_a_time_outside_48_bits_is_refused(now_ms: int) -> None:
    with pytest.raises(ValueError, match="48 bits"):
        ids.new_ulid(now_ms=now_ms)


VALID = "01J9Z3K4M5N6P7Q8R9S0T1V2W3"
NOT_ULIDS = [
    "",
    VALID[:-1],
    VALID + "X",
    VALID + "\n",
    VALID + " ",
    " " + VALID,
    VALID.lower(),
    "8" + VALID[1:],  # more than 48 bits of time
    VALID[:-1] + "I",  # not in Crockford's alphabet
    VALID[:-1] + "L",
    VALID[:-1] + "O",
    VALID[:-1] + "U",
    VALID[:-1] + "-",
]


@pytest.mark.parametrize("value", NOT_ULIDS)
def test_is_ulid_refuses_other_shapes(value: str) -> None:
    assert ids.is_ulid(VALID)
    assert not ids.is_ulid(value)


def test_is_ulid_refuses_a_value_that_is_not_text() -> None:
    assert not ids.is_ulid(None)
    assert not ids.is_ulid(12345)


@pytest.mark.parametrize("value", [VALID, ids.new_ulid(), *NOT_ULIDS])
def test_the_sql_check_agrees_with_the_python_check(value: str) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        # The check is fixed text from vault.db.ids; the value goes in as a parameter.
        sql = f"WITH t(v) AS (VALUES (?)) SELECT {ids.ulid_check('v')} FROM t"  # noqa: S608
        (verdict,) = connection.execute(sql, (value,)).fetchone()
    finally:
        connection.close()
    assert bool(verdict) == ids.is_ulid(value)


# --- what an import writes ---------------------------------------------------------------------


def test_a_document_with_a_version_and_a_source_is_stored(engine: Engine) -> None:
    document_id, version_id = add_document(engine)
    with engine.connect() as connection:
        row = connection.execute(select(documents).where(documents.c.id == document_id)).one()
        source = connection.execute(select(import_sources)).one()
    assert row.current_version_id == version_id
    assert row.local_only is False
    assert row.trashed_at is None
    assert json.loads(source.folder_segments) == ["Users", "Owner", "Downloads"]


def test_a_second_import_of_the_same_bytes_adds_a_source_not_a_document(engine: Engine) -> None:
    document_id, _ = add_document(engine)
    with engine.begin() as connection:
        connection.execute(insert(import_sources).values(source_row(document_id, method="folder")))
    assert count(engine, documents) == 1
    assert count(engine, blobs) == 1
    assert count(engine, import_sources) == 2


def test_two_documents_may_share_a_blob(engine: Engine) -> None:
    add_document(engine)
    add_document(engine)
    assert count(engine, blobs) == 1
    assert count(engine, document_versions) == 2


def test_deleting_a_document_deletes_its_versions_and_sources(engine: Engine) -> None:
    document_id, _ = add_document(engine)
    with engine.begin() as connection:
        connection.execute(documents.delete().where(documents.c.id == document_id))
    assert count(engine, documents) == 0
    assert count(engine, document_versions) == 0
    assert count(engine, import_sources) == 0
    assert count(engine, blobs) == 1  # the blob goes only through the blob store's own delete


def test_a_blob_that_a_version_uses_cannot_be_deleted(engine: Engine) -> None:
    add_document(engine)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as connection:
        connection.execute(blobs.delete())


def test_the_current_version_cannot_be_deleted_from_under_its_document(engine: Engine) -> None:
    _, version_id = add_document(engine)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as connection:
        connection.execute(document_versions.delete().where(document_versions.c.id == version_id))


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("documents", "current_version_id", ids.new_ulid()),
        ("document_versions", "document_id", ids.new_ulid()),
        ("document_versions", "blob_sha256", OTHER_SHA),
        ("import_sources", "document_id", ids.new_ulid()),
    ],
)
def test_a_reference_to_nothing_is_refused(
    engine: Engine, table: str, column: str, value: str
) -> None:
    document_id, _ = add_document(engine)
    rows = {
        "documents": (documents, document_row()),
        "document_versions": (document_versions, version_row(document_id, version_no=2)),
        "import_sources": (import_sources, source_row(document_id)),
    }
    target, row = rows[table]
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as connection:
        connection.execute(insert(target).values(row | {column: value}))


def test_a_version_number_is_used_once_per_document(engine: Engine) -> None:
    document_id, _ = add_document(engine)
    with pytest.raises(IntegrityError, match="UNIQUE"), engine.begin() as connection:
        connection.execute(insert(document_versions).values(version_row(document_id)))


# --- values of the wrong shape -----------------------------------------------------------------

BAD_BLOBS = [
    {"sha256": SHA.upper()},
    {"sha256": SHA[:-1]},
    {"sha256": SHA + "a"},
    {"sha256": SHA[:-1] + "g"},
    {"size": -1},
    {"ext": ""},
    {"ext": ".pdf"},
    {"ext": "PDF"},
    {"ext": "a" * 11},
    {"mime": ""},
    {"mime": "Application/PDF"},
    {"mime": "text/plain; charset=utf-8"},
]


@pytest.mark.parametrize("change", BAD_BLOBS)
def test_a_blob_of_the_wrong_shape_is_refused(engine: Engine, change: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(insert(blobs).values(blob_row(**change)))


def test_every_type_the_validator_accepts_fits_the_blob_table(engine: Engine) -> None:
    with engine.begin() as connection:
        for number, ((extension, _kind), mime) in enumerate(sorted(MIME_TYPES.items())):
            sha = f"{number:064x}"
            connection.execute(insert(blobs).values(blob_row(sha256=sha, ext=extension, mime=mime)))


@pytest.mark.parametrize(
    "change",
    [
        {"id": VALID.lower()},
        {"id": VALID + "\n"},
        {"id": "1"},
        {"state": "trashed"},
        {"state": "Ready"},
        {"state": ""},
    ],
)
def test_a_document_of_the_wrong_shape_is_refused(engine: Engine, change: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(insert(documents).values(document_row(**change)))


@pytest.mark.parametrize("value", [2, -1, "yes"])
def test_a_local_only_flag_other_than_0_or_1_is_refused(engine: Engine, value: object) -> None:
    """SQLAlchemy turns any truthy value into 1, so this writes the raw value with plain SQL."""
    row = document_row()
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents (id, state, local_only, created_at)"
                " VALUES (:id, 'processing', :flag, '2026-10-15 12:00:00')"
            ),
            {"id": row["id"], "flag": value},
        )


@pytest.mark.parametrize(
    "change",
    [{"id": "x"}, {"version_no": 0}, {"version_no": -1}, {"origin": "upload"}, {"origin": ""}],
)
def test_a_version_of_the_wrong_shape_is_refused(engine: Engine, change: dict[str, Any]) -> None:
    document_id, _ = add_document(engine)
    row = version_row(document_id, version_no=2) | change
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(insert(document_versions).values(row))


@pytest.mark.parametrize(
    "change",
    [
        {"id": "x"},
        {"import_batch_id": "batch-1"},
        {"original_filename": ""},
        {"original_path": ""},
        {"folder_segments": "not json"},
        {"folder_segments": json.dumps({"School": "CSE485"})},
        {"folder_segments": json.dumps("School")},
        {"method": "email"},
        {"method": ""},
    ],
)
def test_a_source_of_the_wrong_shape_is_refused(engine: Engine, change: dict[str, Any]) -> None:
    document_id, _ = add_document(engine)
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(insert(import_sources).values(source_row(document_id, **change)))


def test_every_listed_value_is_accepted(engine: Engine) -> None:
    """The lists in vault.db.schema and the CHECK constraints the migration wrote agree."""
    document_id, _ = add_document(engine)
    with engine.begin() as connection:
        for state in DOCUMENT_STATES:
            connection.execute(insert(documents).values(document_row(state=state)))
        for number, origin in enumerate(VERSION_ORIGINS, start=2):
            connection.execute(
                insert(document_versions).values(
                    version_row(document_id, version_no=number, origin=origin)
                )
            )
        for method in IMPORT_METHODS:
            connection.execute(
                insert(import_sources).values(source_row(document_id, method=method))
            )


def test_a_browser_upload_may_have_no_path(engine: Engine) -> None:
    document_id, _ = add_document(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(import_sources).values(
                source_row(document_id, original_path=None, folder_segments="[]")
            )
        )
    assert count(engine, import_sources) == 2


def test_an_empty_folder_list_and_unicode_names_are_kept(engine: Engine) -> None:
    document_id, _ = add_document(engine)
    name = "R" + chr(0xE9) + "sum" + chr(0xE9) + " " + chr(0x4E2D) + chr(0x1F4C4) + ".pdf"
    with engine.begin() as connection:
        connection.execute(
            insert(import_sources).values(
                source_row(document_id, original_filename=name, folder_segments="[]")
            )
        )
        stored: str = connection.execute(
            select(import_sources.c.original_filename).where(
                import_sources.c.original_filename == name
            )
        ).scalar_one()
    assert stored == name


# --- the migration -----------------------------------------------------------------------------


def test_the_document_tables_can_be_undone(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    command.downgrade(alembic_config(db_file), "0001")
    engine = create_vault_engine(db_file)
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version", "app_meta"}
    finally:
        engine.dispose()
    upgrade_to_latest(db_file)  # and done again


def test_the_check_constraints_in_the_migration_match_the_schema(engine: Engine) -> None:
    """Alembic's comparison does not look at CHECK constraints, so compare them by name and text."""
    inspector = inspect(engine)
    for table in (blobs, documents, document_versions, import_sources):
        declared = {
            f"ck_{table.name}_{constraint.name}": " ".join(str(constraint.sqltext).split())
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        stored = {
            str(check["name"]): " ".join(str(check["sqltext"]).split())
            for check in inspector.get_check_constraints(table.name)
        }
        assert declared
        assert stored == declared, table.name
