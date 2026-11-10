"""Deterministic metadata from the import path: folder labels and the title from the file name."""

from __future__ import annotations

import json
import logging
import random
import sys
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from vault.core.config import Settings
from vault.core.logging import SafeFormatter
from vault.db import ids
from vault.db.documents import delete_permanently, get_document, move_to_trash
from vault.db.engine import create_vault_engine
from vault.db.labels import MAX_PATH_LEVELS, delete_unused_folder_labels, labels_of, name_key
from vault.db.migrate import alembic_config, upgrade_to_latest
from vault.db.schema import (
    MAX_LABEL_CHARS,
    app_meta,
    document_labels,
    documents,
    import_sources,
    labels,
)
from vault.ingest import path_metadata
from vault.ingest.importer import Importer, ImportRequest, Outcome
from vault.ingest.path_metadata import (
    MAX_DEPTH,
    PENDING_KEY,
    Backfill,
    clean,
    folder_names,
    label_pending_sources,
    title_from_filename,
)
from vault.main import create_app
from vault.storage.blobs import BlobStore

NOW = datetime(2026, 10, 15, 12, 0, 0)
MIGRATION = Path(path_metadata.__file__).parents[1] / "db" / "migrations" / "versions"


# --- folder names ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("segments", "expected"),
    [
        (["School", "CSE485", "Assignments"], ("School", "CSE485", "Assignments")),
        ([], ()),
        (["", "  ", "\t"], ()),
        # Both separators, inside a name too, and runs of them.
        (["School/CSE485\\Assignments"], ("School", "CSE485", "Assignments")),
        (["School//", "\\\\CSE485"], ("School", "CSE485")),
        # A drive is where the path is, not a folder.
        (["C:", "Users", "Owner"], ("Users", "Owner")),
        (["c:\\School"], ("School",)),
        (["C:School", "CSE485"], ("School", "CSE485")),
        (["\\School"], ("School",)),
        # UNC shares and device paths.
        (["\\\\server\\share\\School"], ("School",)),
        (["//server/share/School"], ("School",)),
        (["", "", "server", "share", "School"], ("School",)),
        (["\\\\server"], ()),
        (["\\\\?\\C:\\School"], ("School",)),
        (["\\\\?\\UNC\\server\\share\\School"], ("School",)),
        (["//?/unc/server/share/School"], ("School",)),
        (["\\\\.\\pipe\\School"], ("School",)),
        # Windows drops trailing dots and spaces; ".", ".." and "..." are no folder.
        ([".", "School.", "..", "...", "CSE 485 . "], ("School", "CSE 485")),
        ([".hidden"], (".hidden",)),
        # White space, control and bidirectional characters, unpaired surrogates.
        ([" Spring \t 2026 "], ("Spring 2026",)),
        (["Note\x00s\x1f", "\u202egpj.exe", "x\u2066y\u2069"], ("Notes", "gpj.exe", "xy")),
        (["\ud800", "\udfffSchool"], ("School",)),
        (
            ["\ufeffSchool\u200b", "Pa\u00adpers", "\U0001f468\u200d\U0001f4bb"],
            ("School", "Papers", "\U0001f468\u200d\U0001f4bb"),
        ),
        (["Line\nbreak\r"], ("Line break",)),
        # One spelling of each letter, whatever the source system wrote.
        (["Cafe\u0301"], ("Caf\u00e9",)),
        (["\u5b66\u6821", "\U0001f4da Books"], ("\u5b66\u6821", "\U0001f4da Books")),
    ],
)
def test_folder_names(segments: list[str], expected: tuple[str, ...]) -> None:
    assert folder_names(segments) == expected


def test_a_long_name_is_cut_to_the_label_limit() -> None:
    assert folder_names(["a" * 400]) == ("a" * MAX_LABEL_CHARS,)
    # Cut, then trailing dots and spaces dropped again.
    assert folder_names(["a" * (MAX_LABEL_CHARS - 1) + " b"]) == ("a" * (MAX_LABEL_CHARS - 1),)


def test_a_deep_path_keeps_its_outermost_folders() -> None:
    names = [f"level{number}" for number in range(MAX_DEPTH * 2)]
    assert folder_names(names) == tuple(names[:MAX_DEPTH])


ALPHABET = (
    "aZ09 ._-:\\/?\t\n\r\x00\x7f\u00a0\u0301\u00e9\u00df\u0130\u0131\u202e\u2066\u200b"
    "\u3000\u5b66\ud800\U0001f4da"
)


def test_every_name_is_one_a_label_can_hold() -> None:
    """Random paths from the awkward characters above: each name that comes out is clean, has no
    separator, no trailing dot or space, and fits a label."""
    rng = random.Random(485)  # noqa: S311 - test data, not security
    for _ in range(5000):
        segments = [
            "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(0, 12)))
            for _ in range(rng.randrange(0, 6))
        ]
        for name in folder_names(segments):
            assert 1 <= len(name) <= MAX_LABEL_CHARS
            assert clean(name) == name
            assert unicodedata.is_normalized("NFC", name)
            assert not set(name) & set("\\/")
            assert not name.endswith((".", " "))
            assert len(name_key(name)) == len(name)


# --- titles and keys ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Lab_report_3.pdf", "Lab report 3"),
        ("CSE485-Syllabus.docx", "CSE485-Syllabus"),
        ("archive.tar.gz", "archive.tar"),
        ("README", "README"),
        (".pdf", ".pdf"),
        ("notes .md", "notes"),
        ("___.pdf", None),
        ("  .txt", ".txt"),
        ("Report\u202e.pdf", "Report"),
        ("Cafe\u0301 menu.txt", "Caf\u00e9 menu"),
        ("a" * 300 + ".txt", "a" * MAX_LABEL_CHARS),
    ],
)
def test_title_from_filename(filename: str, expected: str | None) -> None:
    assert title_from_filename(filename) == expected


def test_a_name_key_is_as_long_as_the_name_for_every_character() -> None:
    """The table checks that the key is exactly as long as the name."""
    for code in range(sys.maxunicode + 1):
        if 0xD800 <= code <= 0xDFFF:
            continue
        assert len(name_key(chr(code))) == 1, hex(code)


def test_the_name_key_folds_case_as_windows_does() -> None:
    assert name_key("School") == name_key("SCHOOL") == name_key("school")
    assert name_key("\u00e4rger") == name_key("\u00c4RGER")
    assert name_key("Stra\u00dfe") != name_key("Strasse")  # Windows keeps these apart


# --- the importer ------------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


class Imports:
    def __init__(self, engine: Engine, tmp_path: Path) -> None:
        self.engine = engine
        blob_root = tmp_path / "blobs"
        blob_root.mkdir()
        self.importer = Importer(engine, BlobStore(blob_root), clock=lambda: NOW)
        self.folder = tmp_path / "incoming"
        self.folder.mkdir()

    def add(self, body: str, *folders: str, name: str = "notes.txt") -> tuple[Outcome, str]:
        path = self.folder / f"{ids.new_ulid()}.txt"
        path.write_text(body, encoding="utf-8")
        result = self.importer.import_file(
            ImportRequest(
                path=path,
                original_filename=name,
                method="folder",
                batch_id=ids.new_ulid(),
                folder_segments=folders,
            )
        )
        assert result.document_id is not None
        return result.outcome, result.document_id

    def paths(self, document_id: str) -> list[tuple[str, ...]]:
        with self.engine.connect() as connection:
            return [label.path for label in labels_of(connection, document_id)]

    def label_count(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(labels)).scalar_one()


@pytest.fixture
def imports(engine: Engine, tmp_path: Path) -> Imports:
    return Imports(engine, tmp_path)


def test_an_imports_folders_become_nested_labels(imports: Imports) -> None:
    _, document_id = imports.add("one\n", "School", "CSE485", "Assignments")
    assert imports.paths(document_id) == [
        ("School",),
        ("School", "CSE485"),
        ("School", "CSE485", "Assignments"),
    ]
    with imports.engine.connect() as connection:
        found = labels_of(connection, document_id)
    assert {(label.kind, label.source, label.status) for label in found} == {
        ("folder", "import_path", "accepted")
    }


def test_an_upload_without_folders_gets_no_labels(imports: Imports) -> None:
    _, document_id = imports.add("one\n")
    assert imports.paths(document_id) == []
    assert imports.label_count() == 0


def test_a_second_import_adds_its_own_folders_once(imports: Imports) -> None:
    _, document_id = imports.add("same\n", "School", "CSE485")
    outcome, again = imports.add("same\n", "Backup", "CSE485")
    assert (outcome, again) == (Outcome.DUPLICATE, document_id)
    imports.add("same\n", "school", "cse485")  # the same folders as Windows sees them
    assert imports.paths(document_id) == [
        ("Backup",),
        ("Backup", "CSE485"),
        ("School",),
        ("School", "CSE485"),
    ]
    assert imports.label_count() == 4


def test_documents_from_one_folder_share_its_labels(imports: Imports) -> None:
    _, first = imports.add("one\n", "School", "Assignments")
    _, second = imports.add("two\n", "SCHOOL", "Assignments")
    _, third = imports.add("three\n", "Work", "Assignments")
    assert imports.paths(first) == imports.paths(second)
    assert imports.paths(third) == [("Work",), ("Work", "Assignments")]
    # School, School/Assignments, Work, Work/Assignments: the same name in two places is two.
    assert imports.label_count() == 4
    with imports.engine.connect() as connection:
        shown = {label.name for label in labels_of(connection, second)}
    assert shown == {"School", "Assignments"}  # the first spelling stays


def test_deleting_a_document_removes_only_the_labels_nobody_else_uses(imports: Imports) -> None:
    _, kept = imports.add("one\n", "School", "CSE485")
    _, gone = imports.add("two\n", "School", "CSE485", "Private", "Deeper")
    assert move_to_trash(imports.engine, gone, NOW)
    delete_permanently(imports.engine, gone)
    with imports.engine.connect() as connection:
        names: set[str] = set(connection.execute(select(labels.c.name)).scalars())
    assert names == {"School", "CSE485"}
    assert imports.paths(kept) == [("School",), ("School", "CSE485")]


def test_an_unused_label_with_a_used_child_stays(engine: Engine) -> None:
    """Only a hand edit can leave a parent unassigned under an assigned child; the clean-up must
    not fail the delete for it."""
    parent, child = ids.new_ulid(), ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(insert(labels).values(label(parent, "Top")))
        connection.execute(insert(labels).values(label(child, "Below", parent_id=parent)))
        document_id = add_bare_document(connection)
        connection.execute(insert(document_labels).values(assignment(document_id, child)))
        assert delete_unused_folder_labels(connection) == 0
        connection.execute(delete(document_labels))
        assert delete_unused_folder_labels(connection) == 2


def test_a_parent_loop_written_by_hand_does_not_hang_the_reader(engine: Engine) -> None:
    first, second = ids.new_ulid(), ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(text("PRAGMA defer_foreign_keys = ON"))
        connection.execute(insert(labels).values(label(first, "A", parent_id=second)))
        connection.execute(insert(labels).values(label(second, "B", parent_id=first)))
        document_id = add_bare_document(connection)
        connection.execute(insert(document_labels).values(assignment(document_id, first)))
        found = labels_of(connection, document_id)
    assert len(found) == 1
    assert len(found[0].path) == MAX_PATH_LEVELS


def test_nothing_about_a_folder_is_logged(
    imports: Imports, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        imports.add("one\n", "Clients", "Patient-Name", name="Diagnosis.txt")
    lines = [SafeFormatter().format(record) for record in caplog.records]
    assert not any(word in line for line in lines for word in ("Clients", "Patient", "Diagnosis"))


# --- the table ---------------------------------------------------------------------------------


def label(label_id: str | None = None, name: str = "School", **changes: Any) -> dict[str, Any]:
    return {
        "id": label_id or ids.new_ulid(),
        "kind": "folder",
        "name": name,
        "name_key": name_key(name),
        "parent_id": None,
        "local_only": False,
    } | changes


def assignment(document_id: str, label_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "label_id": label_id,
        "source": "import_path",
        "status": "accepted",
        "created_at": NOW,
    } | changes


def add_bare_document(connection: Any) -> str:
    """A document row alone: enough for an assignment to point at."""
    document_id = ids.new_ulid()
    connection.execute(
        insert(documents).values(
            id=document_id, state="processing", local_only=False, created_at=NOW
        )
    )
    return document_id


@pytest.mark.parametrize(
    "change",
    [
        {"id": "not-a-ulid"},
        {"kind": "folders"},
        {"name": "", "name_key": ""},
        {"name": "a" * (MAX_LABEL_CHARS + 1), "name_key": "A" * (MAX_LABEL_CHARS + 1)},
        {"name_key": "SCHOOLS"},
        {"color": "red"},
        {"color": "#ABCDEF"},
    ],
)
def test_a_label_of_the_wrong_shape_is_refused(engine: Engine, change: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(labels).values(label(**change)))


def test_a_label_cannot_be_its_own_parent_or_repeat_a_name(engine: Engine) -> None:
    top = ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(insert(labels).values(label(top)))
        connection.execute(insert(labels).values(label(name="Under", parent_id=top)))
        connection.execute(insert(labels).values(label(kind="tag")))  # another kind: allowed
    for row in (
        label(name="SCHOOL"),
        label(name="under", parent_id=top),
        label(parent_id=ids.new_ulid()),
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert(labels).values(row))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(update(labels).values(parent_id=top).where(labels.c.id == top))


@pytest.mark.parametrize(
    "change",
    [
        {"source": "folder"},
        {"status": "pending"},
        {"status": "suggested"},  # an import path is a fact
        {"model": "some-model"},
        {"confidence": 0.5},
        {"source": "ai", "status": "suggested", "confidence": 1.5},
        {"source": "ai", "status": "suggested", "model": ""},
    ],
)
def test_an_assignment_of_the_wrong_shape_is_refused(
    engine: Engine, change: dict[str, Any]
) -> None:
    label_id = ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(insert(labels).values(label(label_id)))
        document_id = add_bare_document(connection)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(document_labels).values(assignment(document_id, label_id, **change))
        )


def test_an_ai_suggestion_names_its_model(engine: Engine) -> None:
    label_id = ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(insert(labels).values(label(label_id, kind="tag")))
        document_id = add_bare_document(connection)
        connection.execute(
            insert(document_labels).values(
                assignment(
                    document_id,
                    label_id,
                    source="ai",
                    status="suggested",
                    model="m",
                    confidence=0.5,
                )
            )
        )


# --- sources recorded before labels ------------------------------------------------------------


def test_the_migration_and_the_application_name_the_same_marker() -> None:
    assert f'PENDING_KEY = "{PENDING_KEY}"' in (MIGRATION / "0006_labels.py").read_text("utf-8")


def add_old_source(engine: Engine, folders: str) -> str:
    with engine.begin() as connection:
        document_id = add_bare_document(connection)
        connection.execute(
            insert(import_sources).values(
                id=ids.new_ulid(),
                document_id=document_id,
                original_filename="old.txt",
                folder_segments=folders,
                import_batch_id=ids.new_ulid(),
                method="folder",
                imported_at=NOW,
            )
        )
    return document_id


def test_sources_from_before_the_migration_are_labelled_once(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    command.downgrade(alembic_config(db_file), "0005")
    engine = create_vault_engine(db_file)
    try:
        labelled = add_old_source(engine, json.dumps(["School", "CSE485"]))
        damaged = add_old_source(engine, "[1, 2]")
        add_old_source(engine, "[]")
        upgrade_to_latest(db_file)
        assert label_pending_sources(engine, NOW) == Backfill(added=2, skipped=1)
        with engine.connect() as connection:
            assert [label.path for label in labels_of(connection, labelled)] == [
                ("School",),
                ("School", "CSE485"),
            ]
            assert labels_of(connection, damaged) == ()
            assert connection.execute(select(app_meta.c.key)).scalars().all() == []
        assert label_pending_sources(engine, NOW) is None
        # Undoing the migration removes the tables and the marker; doing it again brings both.
        command.downgrade(alembic_config(db_file), "0005")
        upgrade_to_latest(db_file)
        assert label_pending_sources(engine, NOW) == Backfill(added=2, skipped=1)
    finally:
        engine.dispose()


def test_the_app_labels_old_sources_at_start_up(
    vault_dir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.model_validate({"vault": {"path": str(vault_dir)}})
    engine: Engine = create_app(settings).state.engine
    document_id = add_old_source(engine, json.dumps(["Clients", "Patient-Name"]))
    with engine.begin() as connection:
        connection.execute(insert(app_meta).values(key=PENDING_KEY, value="1"))
    engine.dispose()
    capfd.readouterr()

    engine = create_app(settings).state.engine
    try:
        with engine.connect() as connection:
            assert [label.path for label in labels_of(connection, document_id)] == [
                ("Clients",),
                ("Clients", "Patient-Name"),
            ]
    finally:
        engine.dispose()
    out, err = capfd.readouterr()
    assert "labels.backfilled added=2 skipped=0" in out + err
    assert "Patient" not in out + err
    assert "Clients" not in out + err


# --- the document's page -----------------------------------------------------------------------


def test_the_detail_shows_labels_and_a_title(client: TestClient, app: Any, tmp_path: Path) -> None:
    imports = Imports(app.state.engine, tmp_path)
    imports.importer = Importer(app.state.engine, app.state.blobs, clock=lambda: NOW)
    _, document_id = imports.add("one\n", "School", "CSE485", name="Lab_report_3.txt")
    body = client.get(f"/api/documents/{document_id}").json()
    assert body["title"] == {"value": "Lab report 3", "source": "filename"}
    assert [(item["name"], item["path"]) for item in body["labels"]] == [
        ("School", ["School"]),
        ("CSE485", ["School", "CSE485"]),
    ]
    assert {item["source"] for item in body["labels"]} == {"import_path"}

    _, untitled = imports.add("two\n", name="___.txt")
    body = client.get(f"/api/documents/{untitled}").json()
    assert body["title"] is None
    assert body["labels"] == []
    assert get_document(app.state.engine, untitled) is not None
