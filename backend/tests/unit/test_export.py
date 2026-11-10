"""The export (``vault.storage.export``) and its command line (``python -m vault.export``).

Every vault and target is a temporary folder with synthetic documents and synthetic names.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from unittest import mock

import pytest

from vault import export as cli
from vault.api.documents import FieldSource
from vault.db import ids
from vault.db.documents import get_document, move_to_trash
from vault.db.engine import create_vault_engine, write_transaction
from vault.db.labels import MAX_PATH_LEVELS, add_folder_labels
from vault.db.migrate import upgrade_to_latest
from vault.ingest.importer import Importer, ImportRequest
from vault.ingest.path_metadata import FILENAME, PENDING_KEY, label_pending_sources
from vault.storage import export as export_module
from vault.storage.blobs import BlobStore
from vault.storage.export import (
    MAX_FILE_PATH,
    MAX_FOLDER_PATH,
    ExportError,
    ExportResult,
    csv_bytes,
    is_device_name,
    neutralise,
    run_export,
    safe_name,
    units,
)

PRIVATE_NAME = "Patient-Diagnosis-Confidential"
NOW = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Vault:
    """A migrated vault folder that synthetic documents are imported into."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "vault"
        self.root.mkdir()
        upgrade_to_latest(self.root / "vault.db")
        engine = create_vault_engine(self.root / "vault.db")
        try:  # as the app's start-up does after migrating
            label_pending_sources(engine, NOW)
        finally:
            engine.dispose()
        self.incoming = tmp_path / "incoming"
        self.incoming.mkdir()
        self.store = BlobStore(self.root / "blobs")
        self.count = 0

    def add(
        self,
        name: str,
        folders: Sequence[str] = (),
        data: bytes | None = None,
        *,
        path: str | None = None,
    ) -> str:
        self.count += 1
        data = data if data is not None else f"synthetic document {self.count}\n".encode()
        source = self.incoming / f"{self.count}.txt"
        source.write_bytes(data)
        engine = create_vault_engine(self.root / "vault.db")
        try:
            outcome = Importer(engine, self.store).import_file(
                ImportRequest(
                    path=source,
                    original_filename=name,
                    method="folder" if folders else "upload",
                    batch_id=ids.new_ulid(),
                    original_path=path,
                    folder_segments=folders,
                )
            )
        finally:
            engine.dispose()
        assert outcome.reason is None
        assert outcome.document_id is not None
        return outcome.document_id

    def trash(self, document_id: str) -> None:
        engine = create_vault_engine(self.root / "vault.db")
        try:
            assert move_to_trash(engine, document_id, datetime(2026, 11, 1, 8, 0))
        finally:
            engine.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(tmp_path)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "exports" / PRIVATE_NAME


def export(vault: Vault, target: Path, **kwargs: Any) -> ExportResult:
    return run_export(vault.root, target, now=lambda: NOW, **kwargs)


def manifest(target: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    return result


def files_of(target: Path) -> dict[str, str]:
    """The exported file of each document, by its id."""
    return {d["id"]: d["file"] for d in manifest(target)["documents"]}


class TestNames:
    @pytest.mark.parametrize("char", [*'<>:"/\\|?*', "\x00", "\x01", "\x1f", "\n", "\t"])
    def test_a_character_windows_refuses_is_replaced(self, char: str) -> None:
        assert safe_name(export_module._clean(f"a{char}b"), ".txt") == "a_b.txt"

    @pytest.mark.parametrize(("given", "made"), [("note. . ", "note"), ("...", "_"), (" ", "_")])
    def test_trailing_dots_and_spaces_are_dropped(self, given: str, made: str) -> None:
        assert safe_name(export_module._clean(given)) == made

    @pytest.mark.parametrize(
        "name",
        ["CON", "con", "Nul.txt", "aux.tar.gz", "PRN ", "Com1 .txt", "lpt9", "COM0", "LPT\u00b9"],
    )
    def test_a_device_name_gets_a_prefix(self, name: str) -> None:
        assert is_device_name(name)
        made = safe_name(*export_module._split(export_module._clean(name)))
        assert made.startswith("_")
        assert not is_device_name(made)

    @pytest.mark.parametrize("name", ["CONSOLE.txt", "COM10", "nullable", "my CON.txt", "CON_"])
    def test_a_name_that_only_looks_like_a_device_is_kept(self, name: str) -> None:
        assert not is_device_name(name)
        assert safe_name(name) == name

    def test_a_cut_that_leaves_a_device_name_gets_the_prefix(self) -> None:
        assert safe_name("CONSOLE", ".txt", limit=8) == "_CON.txt"

    def test_a_long_name_is_cut_and_keeps_its_extension(self) -> None:
        name = safe_name("x" * 500, ".pdf", " (2)")
        assert units(name) <= export_module.COMPONENT_UNITS
        assert name.endswith("x (2).pdf")

    def test_a_cut_never_splits_a_character(self) -> None:
        emoji = "\U0001f600"
        assert units(emoji) == 2
        name = safe_name(emoji * 100, "", "", limit=11)
        assert name == emoji * 5  # ten units, plus the one kept for a device prefix


class TestTheCsv:
    @pytest.mark.parametrize(
        "cell", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx", "\uff1dSUM(A1)", "\uff20x"]
    )
    def test_a_cell_that_would_be_a_formula_is_text(self, cell: str) -> None:
        assert neutralise(cell) == "'" + cell

    @pytest.mark.parametrize("cell", ["notes.txt", "1 = 1", "", None, 42])
    def test_other_cells_are_kept(self, cell: object) -> None:
        assert neutralise(cell) == ("" if cell is None else str(cell))

    def test_the_file_has_a_byte_order_mark(self) -> None:
        assert csv_bytes([]).startswith(b"\xef\xbb\xbf")


class TestAnExport:
    def test_originals_go_into_their_folders_with_a_manifest(
        self, vault: Vault, target: Path
    ) -> None:
        data = b"Course syllabus\nWeek 1\n"
        nested = vault.add("syllabus.txt", ["School", "CSE485"], data, path="C:\\S\\syllabus.txt")
        flat = vault.add("upload.txt")
        result = export(vault, target)
        assert (result.exported, result.problems, result.renamed) == (2, 0, 0)
        assert (target / "files" / "School" / "CSE485" / "syllabus.txt").read_bytes() == data
        assert (target / "files" / "upload.txt").is_file()
        document = next(d for d in manifest(target)["documents"] if d["id"] == nested)
        assert document["file"] == "files/School/CSE485/syllabus.txt"
        assert document["state"] == "processing"
        assert document["versions"][0]["sha256"] == sha(data)
        assert document["versions"][0]["size"] == len(data)
        source = document["sources"][0]
        assert source["folder_segments"] == ["School", "CSE485"]
        assert source["original_path"] == "C:\\S\\syllabus.txt"
        assert source["imported_at"].endswith("Z")
        assert files_of(target)[flat] == "files/upload.txt"
        assert manifest(target)["exported_at"] == "2026-11-03T09:30:00Z"
        assert sorted(p.name for p in target.parent.iterdir()) == [PRIVATE_NAME]

    def test_every_import_source_is_in_the_manifest_and_the_csv(
        self, vault: Vault, target: Path
    ) -> None:
        data = b"the same bytes twice\n"
        first = vault.add("draft.txt", ["Work"], data)
        assert vault.add("copy.txt", ["Elsewhere"], data) == first
        export(vault, target)
        sources = manifest(target)["documents"][0]["sources"]
        assert [s["original_filename"] for s in sources] == ["draft.txt", "copy.txt"]
        assert files_of(target)[first] == "files/Work/draft.txt"  # the first import decides
        text = (target / "manifest.csv").read_bytes().decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        assert [r["original_filename"] for r in rows] == ["draft.txt", "copy.txt"]
        assert {r["sha256"] for r in rows} == {sha(data)}
        assert rows[1]["folder_segments"] == '["Elsewhere"]'

    def test_a_formula_in_a_name_is_text_in_the_csv_and_exact_in_the_json(
        self, vault: Vault, target: Path
    ) -> None:
        name = '=HYPERLINK("http://example.invalid","x").txt'
        vault.add(name)
        export(vault, target)
        assert manifest(target)["documents"][0]["sources"][0]["original_filename"] == name
        text = (target / "manifest.csv").read_bytes().decode("utf-8-sig")
        row = next(csv.DictReader(io.StringIO(text)))
        assert row["original_filename"] == "'" + name

    def test_the_trash_is_left_out_unless_asked_for(self, vault: Vault, target: Path) -> None:
        kept = vault.add("kept.txt")
        trashed = vault.add("trashed.txt")
        vault.trash(trashed)
        result = export(vault, target)
        assert (result.exported, result.trash_left_out) == (1, 1)
        assert set(files_of(target)) == {kept}
        other = target.parent / "with-trash"
        result = export(vault, other, include_trash=True)
        assert (result.exported, result.trash_left_out) == (2, 0)
        document = next(d for d in manifest(other)["documents"] if d["id"] == trashed)
        assert document["state"] == "trashed"
        assert document["state_before_trash"] == "processing"

    def test_an_empty_vault_exports_an_empty_tree(self, vault: Vault, target: Path) -> None:
        assert export(vault, target).exported == 0
        assert manifest(target)["documents"] == []
        assert list((target / "files").iterdir()) == []


class TestCollisions:
    def test_two_documents_with_one_name_both_survive(self, vault: Vault, target: Path) -> None:
        first = vault.add("notes.txt", ["Lab"])
        second = vault.add("notes.txt", ["Lab"])
        result = export(vault, target)
        assert result.renamed == 1
        assert files_of(target) == {first: "files/Lab/notes.txt", second: "files/Lab/notes (2).txt"}

    def test_names_that_differ_only_in_case_collide(self, vault: Vault, target: Path) -> None:
        vault.add("Notes.txt", ["lab"])
        second = vault.add("NOTES.TXT", ["LAB"])
        third = vault.add("notes.txt", ["Lab. "])  # Windows drops the trailing dot and space
        export(vault, target)
        # One folder and three names, as Windows sees them.
        assert files_of(target)[second] == "files/lab/NOTES (2).TXT"
        assert files_of(target)[third] == "files/lab/notes (3).txt"

    def test_names_that_clean_to_the_same_collide(self, vault: Vault, target: Path) -> None:
        vault.add("a:b.txt")
        second = vault.add("a?b.txt")
        third = vault.add("a_b (2).txt")
        export(vault, target)
        assert files_of(target)[second] == "files/a_b (2).txt"
        assert files_of(target)[third] == "files/a_b (2) (2).txt"

    def test_a_file_and_a_folder_with_one_name_both_survive(
        self, vault: Vault, target: Path
    ) -> None:
        vault.add("Reports")
        inside = vault.add("q1.txt", ["reports"])
        also = vault.add("q2.txt", ["Reports"])
        export(vault, target)
        assert files_of(target)[inside] == "files/reports (2)/q1.txt"
        assert files_of(target)[also] == "files/reports (2)/q2.txt"

    def test_device_names_are_exported_safely(self, vault: Vault, target: Path) -> None:
        name = vault.add("nul.txt", ["CON", "aux "])
        export(vault, target)
        assert files_of(target)[name] == "files/_CON/_aux/_nul.txt"

    def test_the_same_vault_always_gives_the_same_paths(self, vault: Vault, target: Path) -> None:
        for _ in range(3):
            vault.add("same.txt", ["Same"])
        export(vault, target)
        export(vault, target.parent / "again")
        assert files_of(target) == files_of(target.parent / "again")


class TestPathLength:
    def test_every_path_fits_in_max_path(self, vault: Vault, target: Path) -> None:
        deep = [f"folder {n} " + "y" * 60 for n in range(10)]
        for n in range(3):
            vault.add("z" * 300 + f"{n}.txt", deep)
        result = export(vault, target)
        assert result.exported == 3
        assert result.placed_higher == 3
        for path in target.rglob("*"):
            limit = MAX_FOLDER_PATH if path.is_dir() else MAX_FILE_PATH
            assert units(str(path)) <= limit
        assert all(f.endswith(".txt") for f in files_of(target).values())
        segments = manifest(target)["documents"][0]["sources"][0]["folder_segments"]
        assert segments == deep  # the manifest keeps the folders the tree could not

    def test_a_target_too_long_for_file_names_is_refused(
        self, vault: Vault, tmp_path: Path
    ) -> None:
        target = tmp_path / ("t" * max(1, 230 - units(str(tmp_path))))
        with pytest.raises(ExportError, match="too long"):
            export(vault, target)
        assert not target.exists()


class TestTheTarget:
    def test_an_empty_folder_is_used(self, vault: Vault, target: Path) -> None:
        vault.add("a.txt")
        target.mkdir(parents=True)
        assert export(vault, target).exported == 1

    def test_a_folder_with_anything_in_it_is_refused_and_left_alone(
        self, vault: Vault, target: Path
    ) -> None:
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("the owner's")
        with pytest.raises(ExportError, match="not empty"):
            export(vault, target)
        assert [p.name for p in target.iterdir()] == ["keep.txt"]
        assert [p.name for p in target.parent.iterdir()] == [PRIVATE_NAME]

    def test_a_file_in_its_place_is_refused(self, vault: Vault, target: Path) -> None:
        target.parent.mkdir(parents=True)
        target.write_text("x")
        with pytest.raises(ExportError, match="not a folder"):
            export(vault, target)

    @pytest.mark.parametrize("where", ["inside", "git", "relative", "link"])
    def test_the_backup_checks_refuse_it_in_the_export_s_words(
        self, vault: Vault, tmp_path: Path, where: str
    ) -> None:
        if where == "inside":
            target = vault.root / PRIVATE_NAME
        elif where == "git":
            (tmp_path / "repo" / ".git").mkdir(parents=True)
            target = tmp_path / "repo" / PRIVATE_NAME
        elif where == "relative":
            target = Path(PRIVATE_NAME)
        else:
            (tmp_path / "real").mkdir()
            try:
                os.symlink(tmp_path / "real", tmp_path / "linked", target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("this system cannot make symbolic links here")
            target = tmp_path / "linked" / PRIVATE_NAME
        with pytest.raises(ExportError) as raised:
            export(vault, target)
        assert "export folder" in str(raised.value)
        assert "backup" not in str(raised.value)
        assert PRIVATE_NAME not in str(raised.value)

    def test_a_failure_leaves_nothing_under_the_target_or_beside_it(
        self, vault: Vault, target: Path
    ) -> None:
        vault.add("a.txt")
        with (
            mock.patch.object(export_module, "csv_bytes", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            export(vault, target)
        assert list(target.parent.iterdir()) == []

    def test_a_folder_that_appears_during_the_export_is_not_replaced(
        self, vault: Vault, target: Path
    ) -> None:
        def appear(entries: list[dict[str, Any]]) -> bytes:
            target.mkdir()
            (target / "theirs.txt").write_text("another program's")
            return b""

        with (
            mock.patch.object(export_module, "csv_bytes", side_effect=appear),
            pytest.raises(ExportError, match="not empty"),
        ):
            export(vault, target)
        assert [p.name for p in target.iterdir()] == ["theirs.txt"]
        assert [p.name for p in target.parent.iterdir()] == [PRIVATE_NAME]

    def test_leftovers_of_a_killed_export_are_counted(self, vault: Vault, target: Path) -> None:
        (target.parent / ".vault-export-abcd1234").mkdir(parents=True)
        assert export(vault, target).leftovers == 1

    def test_a_vault_without_a_database_is_refused(self, tmp_path: Path, target: Path) -> None:
        (tmp_path / "empty-vault").mkdir()
        with pytest.raises(ExportError, match="no database"):
            run_export(tmp_path / "empty-vault", target)

    def test_on_windows_an_open_export_is_reported(self, vault: Vault, target: Path) -> None:
        walked: list[Path] = []

        def walk(path: Path) -> bool:
            walked.append(path)
            return True

        result = export(vault, target, secure=lambda _p, _c: False, tree=walk)
        assert result.private is False
        assert walked == []
        result = export(
            vault, target.parent / "two", secure=lambda _p, _c: True, tree=lambda _: False
        )
        assert result.private is False


class TestOriginalsAreChecked:
    def test_a_missing_original_is_counted_and_the_rest_go_on(
        self, vault: Vault, target: Path
    ) -> None:
        gone = vault.add("gone.txt", data=b"gone\n")
        kept = vault.add("kept.txt")
        vault.store.delete(sha(b"gone\n"))
        result = export(vault, target)
        assert (result.exported, result.missing) == (1, 1)
        documents = {d["id"]: d for d in manifest(target)["documents"]}
        assert (documents[gone]["file"], documents[gone]["problem"]) == (None, "missing")
        assert documents[kept]["file"] == "files/kept.txt"

    @pytest.mark.parametrize("damage", [b"tampered\n", b"same size?\n", b"short", b""])
    def test_an_original_that_does_not_match_its_hash_or_size_is_not_written(
        self, vault: Vault, target: Path, damage: bytes
    ) -> None:
        data = b"same size!\n"
        vault.add("damaged.txt", data=data)
        blob = vault.store.find(sha(data))
        assert blob is not None
        blob.chmod(0o644)
        blob.write_bytes(damage)
        result = export(vault, target)
        assert (result.exported, result.damaged) == (0, 1)
        assert list((target / "files").iterdir()) == []
        assert manifest(target)["documents"][0]["problem"] == "damaged"

    @pytest.mark.parametrize("change", [-1, 1])
    def test_an_original_whose_record_gives_another_size_is_not_written(
        self, vault: Vault, target: Path, change: int
    ) -> None:
        data = b"right bytes, wrong record\n"
        vault.add("record.txt", data=data)
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("UPDATE blobs SET size = size + ?", (change,))
        connection.close()
        result = export(vault, target)
        assert (result.exported, result.damaged) == (0, 1)
        assert list((target / "files").iterdir()) == []

    def test_an_original_that_cannot_be_read_is_counted(self, vault: Vault, target: Path) -> None:
        vault.add("held.txt")
        with mock.patch.object(Path, "open", side_effect=PermissionError("held")):
            result = export(vault, target)
        assert (result.exported, result.unreadable) == (0, 1)
        assert list((target / "files").iterdir()) == []

    def test_a_name_freed_by_a_damaged_original_is_used_by_the_next(
        self, vault: Vault, target: Path
    ) -> None:
        vault.add("x.txt", data=b"first\n")
        second = vault.add("x.txt", data=b"second\n")
        blob = vault.store.find(sha(b"first\n"))
        assert blob is not None
        blob.chmod(0o644)
        blob.write_bytes(b"FIRST\n")
        export(vault, target)
        assert files_of(target)[second] == "files/x.txt"


class TestHandEditedRecords:
    """A value read back from the database is checked again, and kept in the manifest as it is."""

    def edit(self, vault: Vault, statement: str) -> None:
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(statement)
        connection.close()

    def test_a_folder_list_that_is_not_names_puts_the_file_at_the_top(
        self, vault: Vault, target: Path
    ) -> None:
        document = vault.add("odd.txt", ["Kept"])
        self.edit(vault, "UPDATE import_sources SET folder_segments = '[1, 2]'")
        export(vault, target)
        assert files_of(target)[document] == "files/odd.txt"
        assert manifest(target)["documents"][0]["sources"][0]["folder_segments"] == "[1, 2]"

    def test_a_time_that_is_not_a_time_is_kept_as_it_is(self, vault: Vault, target: Path) -> None:
        vault.add("t.txt")
        self.edit(vault, "UPDATE import_sources SET original_mtime = 'yesterday'")
        export(vault, target)
        assert manifest(target)["documents"][0]["sources"][0]["original_mtime"] == "yesterday"

    def test_a_document_without_an_import_is_named_by_its_id(
        self, vault: Vault, target: Path
    ) -> None:
        document = vault.add("gone.txt")
        self.edit(vault, "DELETE FROM import_sources")
        export(vault, target)
        assert files_of(target)[document] == f"files/{document}.txt"

    def test_a_record_of_the_wrong_shape_is_damaged(self, vault: Vault, target: Path) -> None:
        vault.add("x.txt")
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE blobs SET ext = 'T X'")
        connection.close()
        assert export(vault, target).damaged == 1

    def test_two_names_for_one_hash_are_damaged(self, vault: Vault, target: Path) -> None:
        data = b"one hash\n"
        vault.add("x.txt", data=data)
        blob = vault.store.find(sha(data))
        assert blob is not None
        (blob.parent / (blob.stem + ".md")).write_bytes(data)
        assert export(vault, target).damaged == 1

    def test_a_database_sqlite_cannot_read_is_refused(self, vault: Vault, target: Path) -> None:
        (vault.root / "vault.db").write_bytes(b"not a database" * 100)
        with pytest.raises(ExportError, match="SQLite could not read"):
            export(vault, target)
        assert not target.parent.exists() or list(target.parent.iterdir()) == []


def csv_rows(target: Path) -> list[dict[str, str]]:
    text = (target / "manifest.csv").read_bytes().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def give_labels(vault: Vault, document_id: str, names: Sequence[str]) -> None:
    """Folder labels written as the importer writes them, with names no import path could give
    (a line break), as a later label kind or a hand edit may."""
    engine = create_vault_engine(vault.root / "vault.db")
    try:
        with write_transaction(engine) as connection:
            add_folder_labels(connection, document_id, names, datetime(2026, 11, 2, 8, 0))
    finally:
        engine.dispose()


# Label and title text a spreadsheet or a CSV reader could misread: separators, quotes, line
# breaks, formula starts and letters outside ASCII (built from code points).
HOSTILE = [
    "Fees, 2026",
    'The "final" draft',
    "line\nbreak",
    "cr\rlf\r\nend",
    "=SUM(A1:A9)",
    "+cmd",
    "-2+3",
    "@risk",
    "\uff1dfull width",
    "Z\u00fcrich \u6771\u4eac \U0001f642",
]


class TestTitlesAndLabels:
    def test_each_document_has_its_title_and_folder_labels(
        self, vault: Vault, target: Path
    ) -> None:
        nested = vault.add("Lab_report_3.txt", ["School", "CSE485"])
        flat = vault.add("upload.txt")
        export(vault, target)
        documents = {d["id"]: d for d in manifest(target)["documents"]}
        assert manifest(target)["format_version"] == 2
        assert manifest(target)["labels_complete"] is True
        assert documents[nested]["title"] == {"value": "Lab report 3", "source": "filename"}
        labels = documents[nested]["labels"]
        assert [(label["kind"], label["path"]) for label in labels] == [
            ("folder", ["School"]),
            ("folder", ["School", "CSE485"]),
        ]
        assert [label["name"] for label in labels] == ["School", "CSE485"]
        assert {(label["source"], label["status"]) for label in labels} == {
            ("import_path", "accepted")
        }
        assert documents[flat]["labels"] == []
        rows = {row["document_id"]: row for row in csv_rows(target)}
        assert (rows[nested]["title"], rows[nested]["title_source"]) == ("Lab report 3", "filename")
        assert json.loads(rows[nested]["labels"]) == labels
        assert json.loads(rows[flat]["labels"]) == []

    def test_the_title_and_labels_are_the_ones_the_library_shows(
        self, vault: Vault, target: Path
    ) -> None:
        data = b"one document, two imports\n"
        document = vault.add("b_notes.txt", ["beta", "Inner"], data)
        assert vault.add("other_name.txt", ["Alpha"], data) == document
        give_labels(vault, document, ["alpha", "zeta"])  # under the same Alpha: one label
        export(vault, target)
        exported = manifest(target)["documents"][0]
        engine = create_vault_engine(vault.root / "vault.db")
        try:
            detail = get_document(engine, document)
        finally:
            engine.dispose()
        assert detail is not None
        shown = [(label.id, label.kind, list(label.path)) for label in detail.labels]
        assert [(e["id"], e["kind"], e["path"]) for e in exported["labels"]] == shown
        assert [e["path"] for e in exported["labels"]] == [
            ["Alpha"], ["Alpha", "zeta"], ["beta"], ["beta", "Inner"],
        ]  # fmt: skip
        assert exported["title"]["value"] == "b notes"  # the first import names it
        assert get_args(FieldSource) == (FILENAME,)

    def test_the_order_does_not_depend_on_how_the_rows_were_written(
        self, vault: Vault, tmp_path: Path
    ) -> None:
        document = vault.add("x.txt")
        give_labels(vault, document, ["b"])
        give_labels(vault, document, ["A"])
        give_labels(vault, document, ["c"])
        first, second = tmp_path / "first", tmp_path / "second"
        export(vault, first)
        with sqlite3.connect(vault.root / "vault.db") as connection:
            rows = connection.execute("SELECT * FROM document_labels").fetchall()
            connection.execute("DELETE FROM document_labels")
            connection.executemany(
                "INSERT INTO document_labels VALUES (?, ?, ?, ?, ?, ?, ?)", reversed(rows)
            )
        connection.close()
        export(vault, second)
        assert manifest(first)["documents"] == manifest(second)["documents"]
        assert [e["name"] for e in manifest(first)["documents"][0]["labels"]] == ["A", "b", "c"]
        assert (first / "manifest.csv").read_bytes() == (second / "manifest.csv").read_bytes()

    def test_labels_a_spreadsheet_could_misread_survive_both_manifests(
        self, vault: Vault, target: Path
    ) -> None:
        document = vault.add("x.txt")
        for name in HOSTILE:
            give_labels(vault, document, [name])
        export(vault, target)
        labels = manifest(target)["documents"][0]["labels"]
        assert sorted(label["name"] for label in labels) == sorted(HOSTILE)
        raw = (target / "manifest.csv").read_bytes().decode("utf-8-sig")
        assert raw.count("\r\n") == 2  # the header and one row: no break leaks out of the cell
        assert "\n" not in raw.replace("\r\n", "")
        assert "\r" not in raw.replace("\r\n", "")
        (row,) = csv_rows(target)
        assert json.loads(row["labels"]) == labels
        assert not row["labels"].startswith(export_module.FORMULA_STARTS)

    @pytest.mark.parametrize("folder", [name for name in HOSTILE if "\n" not in name])
    def test_a_folder_label_from_an_import_survives_both_manifests(
        self, vault: Vault, target: Path, folder: str
    ) -> None:
        vault.add("x.txt", [folder])
        export(vault, target)
        (label,) = manifest(target)["documents"][0]["labels"]
        assert label["path"] == [folder]
        (row,) = csv_rows(target)
        assert json.loads(row["labels"]) == [label]

    @pytest.mark.parametrize("stem", [name for name in HOSTILE if not set(name) & set("\r\n")])
    def test_a_title_a_spreadsheet_could_misread_is_text_in_the_csv(
        self, vault: Vault, target: Path, stem: str
    ) -> None:
        vault.add(stem + ".txt")
        export(vault, target)
        assert manifest(target)["documents"][0]["title"] == {"value": stem, "source": "filename"}
        (row,) = csv_rows(target)
        assert row["title"] == neutralise(stem)
        assert row["title_source"] == "filename"
        assert not row["title"].startswith(export_module.FORMULA_STARTS)

    def test_a_document_without_an_import_has_no_title(self, vault: Vault, target: Path) -> None:
        vault.add("gone.txt", ["Kept"])
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("DELETE FROM import_sources")
        connection.close()
        export(vault, target)
        document = manifest(target)["documents"][0]
        assert document["title"] is None
        assert [label["path"] for label in document["labels"]] == [["Kept"]]
        (row,) = csv_rows(target)
        assert (row["title"], row["title_source"]) == ("", "")

    def test_a_vault_the_app_has_not_labelled_yet_says_so(self, vault: Vault, target: Path) -> None:
        vault.add("x.txt", ["Kept"])
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, '1')", (PENDING_KEY,))
        connection.close()
        result = export(vault, target)
        assert result.labels_complete is False
        assert manifest(target)["labels_complete"] is False
        assert [e["path"] for e in manifest(target)["documents"][0]["labels"]] == [["Kept"]]

    def test_a_database_from_before_labels_still_exports(self, vault: Vault, target: Path) -> None:
        document = vault.add("x.txt", ["Kept"])
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TABLE document_labels")
            connection.execute("DROP TABLE labels")
        connection.close()
        result = export(vault, target)
        assert (result.exported, result.labels_complete) == (1, False)
        assert manifest(target)["documents"][0]["labels"] == []
        assert files_of(target)[document] == "files/Kept/x.txt"

    def test_an_assignment_whose_label_is_gone_keeps_its_id(
        self, vault: Vault, target: Path
    ) -> None:
        vault.add("x.txt", ["Gone"])
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            (label_id,) = connection.execute("SELECT id FROM labels").fetchone()
            connection.execute("DELETE FROM labels")
        connection.close()
        export(vault, target)
        (label,) = manifest(target)["documents"][0]["labels"]
        assert label == {
            "id": label_id, "kind": None, "name": None, "path": [],
            "source": "import_path", "status": "accepted",
        }  # fmt: skip

    def test_a_parent_loop_ends_the_path(self, vault: Vault, target: Path) -> None:
        vault.add("x.txt", ["Outer", "Inner"])
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE labels SET parent_id = (SELECT id FROM labels WHERE name = 'Inner') "
                "WHERE name = 'Outer'"
            )
        connection.close()
        export(vault, target)
        paths = [label["path"] for label in manifest(target)["documents"][0]["labels"]]
        assert all(len(path) == MAX_PATH_LEVELS for path in paths)


class TestTheCommandLine:
    @pytest.fixture
    def settings(self, vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "settings.toml"
        path.write_text(f"[vault]\npath = {json.dumps(str(vault.root))}\n")
        monkeypatch.setenv("AIVAULT_CONFIG", str(path))
        monkeypatch.delenv(cli.TARGET_ENV_VAR, raising=False)
        return path

    def private_words(self, vault: Vault, target: Path) -> list[str]:
        return [PRIVATE_NAME, str(target), str(vault.root), "Clinic", "intake"]

    def test_it_prints_counts_and_never_a_name(
        self, settings: Path, vault: Vault, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vault.add("intake.txt", ["Clinic"])
        assert cli.main(["--to", str(target)]) == 0
        out, err = capsys.readouterr()
        assert "Documents exported: 1" in out
        assert err == ""
        for word in self.private_words(vault, target):
            assert word not in out + err

    def test_labels_not_complete_are_a_warning_that_names_none(
        self, settings: Path, vault: Vault, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vault.add("intake.txt", ["Clinic"])
        with sqlite3.connect(vault.root / "vault.db") as connection:
            connection.execute("INSERT INTO app_meta (key, value) VALUES (?, '1')", (PENDING_KEY,))
        connection.close()
        assert cli.main(["--to", str(target)]) == 0
        out, err = capsys.readouterr()
        assert "folder labels are not complete" in out
        for word in self.private_words(vault, target):
            assert word not in out + err

    def test_the_folder_can_come_from_the_environment_and_the_trash_be_added(
        self,
        settings: Path,
        vault: Vault,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vault.trash(vault.add("old.txt"))
        monkeypatch.setenv(cli.TARGET_ENV_VAR, str(target))
        assert cli.main(["--include-trash"]) == 0
        assert "Documents exported: 1" in capsys.readouterr().out

    def test_without_a_folder_it_says_how_to_give_one(
        self, settings: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main([]) == 1
        assert "pass --to" in capsys.readouterr().err

    def test_problems_fail_the_run_without_a_name(
        self, settings: Path, vault: Vault, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vault.add("intake.txt", ["Clinic"], b"gone\n")
        vault.store.delete(sha(b"gone\n"))
        assert cli.main(["--to", str(target)]) == 1
        out, err = capsys.readouterr()
        assert "not in the vault: 1" in out
        assert "manifest.json says which" in err
        for word in self.private_words(vault, target):
            assert word not in out + err

    def test_a_refusal_is_fixed_text(
        self, settings: Path, vault: Vault, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = vault.root / PRIVATE_NAME
        assert cli.main(["--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "inside the vault folder" in err
        for word in self.private_words(vault, target):
            assert word not in err

    @pytest.mark.parametrize(
        "error", [FileNotFoundError(2, "No such file", PRIVATE_NAME), KeyError]
    )
    def test_an_error_this_package_does_not_own_shows_only_its_class(
        self, settings: Path, target: Path, capsys: pytest.CaptureFixture[str], error: Exception
    ) -> None:
        with mock.patch.object(cli, "run_export", side_effect=error):
            assert cli.main(["--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "stopped (" in err
        assert PRIVATE_NAME not in err

    def test_an_interruption_is_reported_without_a_traceback(
        self, settings: Path, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with mock.patch.object(cli, "run_export", side_effect=KeyboardInterrupt):
            assert cli.main(["--to", str(target)]) == cli.EXIT_INTERRUPTED
        assert "interrupted" in capsys.readouterr().err

    def test_settings_that_cannot_be_read_are_named_by_class_only(
        self,
        tmp_path: Path,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("AIVAULT_CONFIG", str(tmp_path / PRIVATE_NAME / "config.toml"))
        assert cli.main(["--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "FileNotFoundError" in err
        assert PRIVATE_NAME not in err

    def test_settings_without_a_vault_are_refused(
        self,
        tmp_path: Path,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "settings.toml"
        path.write_text("[server]\nport = 8765\n")
        monkeypatch.setenv("AIVAULT_CONFIG", str(path))
        assert cli.main(["--to", str(target)]) == 1
        assert "no vault folder is set" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["--to", "D:\\Clients\\" + PRIVATE_NAME, PRIVATE_NAME],
            ["--" + PRIVATE_NAME],
            ["--to"],
            ["--t", PRIVATE_NAME],
            ["--include-trash=" + PRIVATE_NAME],
            [PRIVATE_NAME],
        ],
    )
    def test_a_malformed_command_line_never_repeats_what_was_typed(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == 2
        out, err = capsys.readouterr()
        assert "command line is not valid" in err
        assert PRIVATE_NAME not in out + err

    def test_the_report_names_every_count(self) -> None:
        result = ExportResult(
            exported=1, missing=2, damaged=3, unreadable=4, trash_left_out=5, renamed=6,
            placed_higher=7, leftovers=8, labels_complete=False, private=False,
        )  # fmt: skip
        text = "\n".join(cli.report(result))
        for figure in range(1, 9):
            assert f": {figure}" in text
        assert "Warning: other accounts" in text
        assert "folder labels are not complete" in text
        assert result.problems == 2 + 3 + 4
