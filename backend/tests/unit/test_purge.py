"""Permanent delete: only from the trash, every row of the document, then each blob no row uses,
with the crash window between the two handled by the start-up sweep and races with restore and
import that never lose a blob a live row needs."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text, update

from vault.core.config import Settings
from vault.core.logging import SafeFormatter
from vault.db import ids
from vault.db.chunks import replace_chunks
from vault.db.documents import (
    DOCUMENT_ROWS,
    DeletedRows,
    DeleteRefusal,
    delete_permanently,
    move_to_trash,
    restore,
)
from vault.db.engine import create_vault_engine, write_transaction
from vault.db.pages import PageText, replace_pages
from vault.db.schema import blobs, document_versions, documents, import_sources, metadata
from vault.ingest import importer as importer_module
from vault.ingest.chunking.structural_v1 import CHUNKER_VERSION, chunk_pages
from vault.ingest.importer import Importer, ImportRequest, Outcome
from vault.main import create_app
from vault.storage.blobs import ORPHAN_FOLDER, BlobNotFoundError, BlobStore
from vault.storage.purge import Purged, purge_document, remove_unused_blob

from ..conftest import LOCAL

URL = "/api/documents"
NOW = datetime(2026, 10, 15, 12, 0, 0)
BODY = b"Rent is due on the first of each month.\n"
SHA = hashlib.sha256(BODY).hexdigest()
# A synthetic word that only the chunks of the document made by ``trashed_with_everything`` hold.
MARKER = "zanzibarquokka"


class Vault:
    """The test vault's database and store, with helpers to import and trash synthetic files."""

    def __init__(self, app: FastAPI, folder: Path) -> None:
        self.engine: Engine = app.state.engine
        self.store: BlobStore = app.state.blobs
        self.folder = folder

    def importer(self, engine: Engine | None = None) -> Importer:
        return Importer(engine or self.engine, self.store)

    def add(self, body: bytes = BODY, name: str = "Patient-Lease.txt") -> str:
        result = self.importer().import_file(self.request(body, name))
        assert result.outcome in {Outcome.IMPORTED, Outcome.DUPLICATE}
        assert result.document_id is not None
        return result.document_id

    def request(self, body: bytes = BODY, name: str = "Patient-Lease.txt") -> ImportRequest:
        path = self.folder / f"incoming-{ids.new_ulid()}{Path(name).suffix}"
        path.write_bytes(body)
        return ImportRequest(
            path=path, original_filename=name, method="upload", batch_id=ids.new_ulid()
        )

    def trash(self, document_id: str) -> None:
        assert move_to_trash(self.engine, document_id, NOW)

    def count(self, table: str, document_id: str) -> int:
        """Rows of ``table`` that belong to the document (through its versions for ``pages``)."""
        named = metadata.tables[table]
        with self.engine.connect() as connection:
            if table == "documents":
                where = named.c.id == document_id
            elif table == "pages":  # chunks have their own document_id
                where = named.c.version_id.in_(
                    select(document_versions.c.id).where(
                        document_versions.c.document_id == document_id
                    )
                )
            else:
                where = named.c.document_id == document_id
            return connection.execute(
                select(func.count()).select_from(named).where(where)
            ).scalar_one()

    def add_chunks(self, document_id: str, page_text: str) -> None:
        """Pages and chunks for the document's current version, as the chunk stage will write."""
        version_id = self.version_of(document_id)
        pages_in = [PageText(1, page_text)]
        replace_pages(self.engine, version_id, pages_in, "text-1", None)
        replace_chunks(self.engine, version_id, chunk_pages(pages_in), CHUNKER_VERSION)

    def search(self, query: str) -> list[str]:
        """The documents whose chunks match an FTS5 query, one entry per matching chunk."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT chunks.document_id FROM chunks_fts"
                    " JOIN chunks ON chunks.id = chunks_fts.rowid WHERE chunks_fts MATCH :query"
                ),
                {"query": query},
            )
            return [row[0] for row in rows]

    def index_rows(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT count(*) FROM chunks_fts")).scalar_one()

    def blob_row(self, sha256: str = SHA) -> bool:
        with self.engine.connect() as connection:
            found = connection.execute(select(blobs.c.sha256).where(blobs.c.sha256 == sha256))
            return found.first() is not None

    def version_of(self, document_id: str) -> str:
        with self.engine.connect() as connection:
            return connection.execute(
                select(documents.c.current_version_id).where(documents.c.id == document_id)
            ).scalar_one()

    def share_blob(self, sha256: str = SHA) -> str:
        """Another document whose own version holds the same bytes (a later phase's user version
        or AI copy can do this; the importer gives the same bytes one document)."""
        document_id, version_id = ids.new_ulid(), ids.new_ulid()
        with self.engine.begin() as connection:
            connection.execute(
                documents.insert().values(
                    id=document_id, state="ready", local_only=False, created_at=NOW
                )
            )
            connection.execute(
                document_versions.insert().values(
                    id=version_id,
                    document_id=document_id,
                    blob_sha256=sha256,
                    version_no=1,
                    origin="user",
                    created_at=NOW,
                )
            )
            connection.execute(
                update(documents)
                .where(documents.c.id == document_id)
                .values(current_version_id=version_id)
            )
        return document_id


@pytest.fixture
def vault(app: FastAPI, tmp_path: Path) -> Vault:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Vault(app, folder)


def trashed_with_everything(vault: Vault) -> str:
    """A trashed document with a version, an import source, a job, pages and chunks."""
    document_id = vault.add()
    vault.add_chunks(document_id, f"Rent and the {MARKER} clause.\n\nPaid monthly.")
    vault.trash(document_id)
    for table in DOCUMENT_ROWS:
        assert vault.count(table, document_id) >= 1, table
    return document_id


# --- the endpoint ------------------------------------------------------------------------------


def test_a_trashed_document_is_deleted_with_every_row_and_its_file(
    client: TestClient, vault: Vault
) -> None:
    document_id = trashed_with_everything(vault)
    path = vault.store.find(SHA)
    assert path is not None

    response = client.delete(f"{URL}/{document_id}")

    assert response.status_code == 204, response.text
    assert response.content == b""
    for table in DOCUMENT_ROWS:
        assert vault.count(table, document_id) == 0, table
    assert not vault.blob_row()
    assert vault.store.find(SHA) is None
    assert not path.exists()
    assert client.get(f"{URL}/{document_id}").status_code == 404
    assert client.get(URL, params={"trash": True}).json()["total"] == 0


def test_a_deleted_documents_text_is_no_longer_found(client: TestClient, vault: Vault) -> None:
    """Its chunks and their keyword index rows go in the delete's transaction; another
    document's chunks and index rows stay."""
    other_body = b"Another synthetic note.\n"
    kept = vault.add(other_body, "note.txt")
    vault.add_chunks(kept, "The other note mentions rent too.")
    document_id = trashed_with_everything(vault)
    assert vault.search(MARKER) == [document_id]
    assert sorted(set(vault.search("rent"))) == sorted({document_id, kept})
    kept_rows = vault.count("chunks", kept)

    assert client.delete(f"{URL}/{document_id}").status_code == 204

    assert vault.count("chunks", document_id) == 0
    assert vault.search(MARKER) == []
    assert vault.search("rent") == [kept]
    assert vault.count("chunks", kept) == kept_rows == vault.index_rows()
    with vault.engine.begin() as connection:
        connection.execute(text("INSERT INTO chunks_fts (chunks_fts) VALUES ('integrity-check')"))


def test_deleting_again_is_a_404(client: TestClient, vault: Vault) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    assert client.delete(f"{URL}/{document_id}").status_code == 204
    second = client.delete(f"{URL}/{document_id}")
    assert second.status_code == 404
    assert second.json() == {"detail": "no such document"}


@pytest.mark.parametrize("state", ["processing", "searchable", "ready", "failed"])
def test_a_document_in_use_is_refused_and_kept(
    client: TestClient, vault: Vault, state: str
) -> None:
    document_id = vault.add()
    with vault.engine.begin() as connection:
        connection.execute(
            update(documents).where(documents.c.id == document_id).values(state=state)
        )
    response = client.delete(f"{URL}/{document_id}")
    assert response.status_code == 409
    assert response.json() == {"detail": "the document is not in the trash"}
    for table in ("jobs", "import_sources", "document_versions", "documents"):
        assert vault.count(table, document_id) == 1, table
    assert vault.blob_row()
    assert vault.store.find(SHA) is not None
    assert client.get(f"{URL}/{document_id}").json()["state"] == state


@pytest.mark.parametrize(
    "document_id", ["01J9ZZZZZZZZZZZZZZZZZZZZZZ", "Patient-Name", "a" * 26, "..%2Fvault.db"]
)
def test_an_unknown_document_is_a_404_that_does_not_repeat_the_id(
    client: TestClient, vault: Vault, document_id: str
) -> None:
    vault.trash(vault.add())
    response = client.delete(f"{URL}/{document_id}")
    assert response.status_code == 404
    assert "Patient" not in response.text
    assert "vault" not in response.text
    assert vault.store.find(SHA) is not None


def test_the_delete_needs_the_token(anonymous: TestClient, vault: Vault) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    response = anonymous.delete(f"{URL}/{document_id}", headers={"Origin": LOCAL})
    assert response.status_code == 401
    assert vault.count("documents", document_id) == 1


def test_the_delete_refuses_another_origin(app: FastAPI, token: str, vault: Vault) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    other = TestClient(
        app,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {token}", "Origin": "http://evil.example"},
    )
    assert other.delete(f"{URL}/{document_id}").status_code == 403
    assert vault.count("documents", document_id) == 1


def test_without_a_vault_there_is_nothing_to_delete() -> None:
    app = create_app(Settings())
    client = TestClient(
        app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    )
    assert client.delete(f"{URL}/{ids.new_ulid()}").status_code == 503


def test_the_delete_logs_counts_and_no_name_id_or_hash(
    client: TestClient, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    document_id = vault.add(name="Diagnosis-Patient.txt")
    vault.trash(document_id)
    caplog.clear()
    assert client.delete(f"{URL}/{document_id}").status_code == 204
    lines = [
        SafeFormatter().format(record)
        for record in caplog.records
        if record.name.startswith("vault.")
    ]
    assert any(
        "documents.deleted blobs_removed=1 blobs_kept=0 blobs_left=0" in line for line in lines
    )
    for private in ("Diagnosis", "Patient", document_id, SHA, "Rent"):
        assert not any(private in line for line in lines)


# --- what goes and what stays ------------------------------------------------------------------


def test_every_table_that_points_at_a_document_or_version_is_cleared() -> None:
    """``DOCUMENT_ROWS`` must name every table with a foreign key to a document or a version, so
    a table added later cannot keep rows (or block the delete) unnoticed."""
    pointing = {
        table.name
        for table in metadata.tables.values()
        for key in table.foreign_keys
        if key.column.table.name in {"documents", "document_versions"}
    }
    assert set(DOCUMENT_ROWS) == pointing | {"documents"}
    # Children before the tables they point at.
    assert DOCUMENT_ROWS.index("chunks") < DOCUMENT_ROWS.index("pages")
    assert DOCUMENT_ROWS.index("pages") < DOCUMENT_ROWS.index("document_versions")
    assert DOCUMENT_ROWS[-2:] == ("document_versions", "documents")


def test_a_blob_another_documents_version_uses_survives(client: TestClient, vault: Vault) -> None:
    document_id = vault.add()
    other = vault.share_blob()
    vault.trash(document_id)

    assert client.delete(f"{URL}/{document_id}").status_code == 204

    assert vault.count("documents", document_id) == 0
    assert vault.blob_row()
    assert vault.store.find(SHA) is not None
    download = client.get(f"{URL}/{other}/file")
    assert download.status_code == 200
    assert download.content == BODY


def test_only_the_deleted_documents_blobs_go(client: TestClient, vault: Vault) -> None:
    other_body = b"Another synthetic note.\n"
    kept = vault.add(other_body, "note.txt")
    document_id = vault.add()
    vault.trash(document_id)
    assert client.delete(f"{URL}/{document_id}").status_code == 204
    assert vault.store.find(hashlib.sha256(other_body).hexdigest()) is not None
    assert client.get(f"{URL}/{kept}/file").content == other_body


def test_the_rows_go_in_one_transaction(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure part of the way through leaves every row as it was."""
    document_id = trashed_with_everything(vault)
    from vault.db import documents as documents_module

    original_delete = sqlalchemy.delete
    calls = {"n": 0}

    def failing_delete(table: Any) -> Any:
        calls["n"] += 1
        if table is document_versions:
            raise RuntimeError
        return original_delete(table)

    monkeypatch.setattr(documents_module, "delete", failing_delete)
    with pytest.raises(RuntimeError):
        delete_permanently(vault.engine, document_id)
    assert calls["n"] >= 4  # chunks, pages, jobs and import sources went before the failure
    for table in DOCUMENT_ROWS:
        assert vault.count(table, document_id) >= 1, table
    assert vault.search(MARKER) == [document_id]  # the index rows came back with the chunks
    assert vault.blob_row()


# --- the crash window --------------------------------------------------------------------------


def restart(vault_dir: Path) -> FastAPI:
    app = create_app(Settings.model_validate({"vault": {"path": str(vault_dir)}}))
    app.state.engine.dispose()
    return app


def test_a_crash_after_the_commit_leaves_the_file_to_the_start_up_sweep(
    vault: Vault, vault_dir: Path
) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    # The process dies after the rows are committed and before the file is removed.
    assert delete_permanently(vault.engine, document_id) == DeletedRows(blobs=(SHA,))
    assert vault.store.find(SHA) is not None
    vault.engine.dispose()

    restart(vault_dir)

    store = BlobStore(vault_dir / "blobs")
    assert store.find(SHA) is None
    assert (vault_dir / "blobs" / ORPHAN_FOLDER / f"{SHA}.txt").read_bytes() == BODY


def test_a_file_that_cannot_be_removed_now_is_left_to_the_sweep(
    client: TestClient, vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = vault.add()
    vault.trash(document_id)

    def refuse(_sha256: str) -> bool:
        raise PermissionError

    monkeypatch.setattr(vault.store, "delete", refuse)
    assert client.delete(f"{URL}/{document_id}").status_code == 204
    assert vault.count("documents", document_id) == 0
    assert vault.store.find(SHA) is not None
    monkeypatch.undo()
    vault.engine.dispose()

    restart(vault_dir)

    assert BlobStore(vault_dir / "blobs").find(SHA) is None
    assert (vault_dir / "blobs" / ORPHAN_FOLDER / f"{SHA}.txt").exists()


def test_a_purge_counts_each_blob_outcome(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    document_id = vault.add()
    vault.trash(document_id)

    def gone(_sha256: str) -> bool:
        raise BlobNotFoundError

    monkeypatch.setattr(vault.store, "delete", gone)
    assert purge_document(vault.engine, vault.store, document_id) == Purged(0, 0, 1)


# --- races -------------------------------------------------------------------------------------


def test_an_import_of_the_same_bytes_after_the_commit_keeps_its_blob(
    client: TestClient, vault: Vault
) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    assert delete_permanently(vault.engine, document_id) == DeletedRows(blobs=(SHA,))

    again = vault.add(name="Lease again.txt")  # between the commit and the file removal

    assert not remove_unused_blob(vault.engine, vault.store, SHA)
    assert again != document_id
    assert vault.store.find(SHA) is not None
    assert client.get(f"{URL}/{again}/file").content == BODY


def test_an_import_whose_copy_was_removed_before_its_rows_copies_it_again(
    client: TestClient, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import finds the file in the store, then the delete removes it before the import's
    rows are written: the import notices under the write lock and stores the bytes again."""
    document_id = vault.add()
    vault.trash(document_id)
    assert delete_permanently(vault.engine, document_id) == DeletedRows(blobs=(SHA,))

    original = Importer._record
    removed: list[bool] = []

    def record_after_removal(self: Importer, *args: Any) -> Any:
        if not removed:
            removed.append(remove_unused_blob(vault.engine, vault.store, SHA))
        return original(self, *args)

    monkeypatch.setattr(Importer, "_record", record_after_removal)
    result = vault.importer().import_file(vault.request())

    assert removed == [True]
    assert result.outcome is Outcome.IMPORTED
    assert vault.blob_row()
    assert vault.store.find(SHA) is not None
    assert vault.store.verify(SHA)
    assert client.get(f"{URL}/{result.document_id}/file").content == BODY


def test_an_import_whose_copy_was_removed_before_its_check_copies_it_again(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same, with the file removed between the copy and the re-hash of the stored copy."""
    document_id = vault.add()
    vault.trash(document_id)
    delete_permanently(vault.engine, document_id)
    original = vault.store.verify
    calls: list[str] = []

    def verify_after_removal(sha256: str) -> bool:
        if not calls:
            calls.append(sha256)
            remove_unused_blob(vault.engine, vault.store, sha256)
        return original(sha256)

    monkeypatch.setattr(vault.store, "verify", verify_after_removal)
    result = vault.importer().import_file(vault.request())
    assert result.outcome is Outcome.IMPORTED
    assert vault.store.find(SHA) is not None


def test_an_import_gives_up_when_the_copy_keeps_disappearing(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault.store, "exists", lambda _sha256: False)
    with pytest.raises(BlobNotFoundError):
        vault.importer().import_file(vault.request())
    with vault.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(documents)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(blobs)).scalar_one() == 0
    assert importer_module.STORE_TRIES == 3


def test_a_restore_first_makes_the_delete_a_refusal(vault: Vault) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    assert restore(vault.engine, document_id)
    result = purge_document(vault.engine, vault.store, document_id)
    assert result is DeleteRefusal.NOT_IN_TRASH
    assert vault.store.find(SHA) is not None


def test_a_delete_first_makes_the_restore_find_nothing(vault: Vault) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    assert isinstance(purge_document(vault.engine, vault.store, document_id), Purged)
    assert not restore(vault.engine, document_id)


def test_an_import_that_lifts_the_document_out_of_the_trash_first_is_kept(vault: Vault) -> None:
    document_id = vault.add()
    vault.trash(document_id)
    assert vault.add(name="Lease copy.txt") == document_id  # back in use
    assert purge_document(vault.engine, vault.store, document_id) is DeleteRefusal.NOT_IN_TRASH
    assert vault.store.find(SHA) is not None


def race(*steps: Callable[[], object]) -> list[object]:
    """Run the steps in threads that start together; return what each returned or raised."""
    start = threading.Barrier(len(steps))
    results: list[object] = [None] * len(steps)

    def run(index: int, step: Callable[[], object]) -> None:
        start.wait()
        try:
            results[index] = step()
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=pair) for pair in enumerate(steps)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def every_recorded_blob_has_its_file(vault: Vault) -> None:
    with vault.engine.connect() as connection:
        recorded: list[str] = list(connection.execute(select(blobs.c.sha256)).scalars())
        used: set[str] = set(connection.execute(select(document_versions.c.blob_sha256)).scalars())
    for sha256 in recorded:
        assert vault.store.find(sha256) is not None
    assert used <= set(recorded)


RACE_ROUNDS = 25


def test_a_delete_racing_a_restore_never_loses_a_live_blob(vault: Vault, db_engine: Engine) -> None:
    for _ in range(RACE_ROUNDS):
        document_id = vault.add()
        vault.trash(document_id)
        deleted, restored = race(
            lambda: purge_document(vault.engine, vault.store, document_id),  # noqa: B023
            lambda: restore(db_engine, document_id),  # noqa: B023
        )
        assert not isinstance(deleted, Exception)
        assert not isinstance(restored, Exception)
        if isinstance(deleted, Purged):  # the delete came first: nothing left to restore
            assert restored is False
            assert vault.store.find(SHA) is None
        else:  # the restore came first: the document is in use with its file
            assert deleted is DeleteRefusal.NOT_IN_TRASH
            assert restored is True
            assert vault.store.find(SHA) is not None
            vault.trash(document_id)
            assert isinstance(purge_document(vault.engine, vault.store, document_id), Purged)
        every_recorded_blob_has_its_file(vault)


def test_a_delete_racing_an_import_of_the_same_bytes_never_loses_its_blob(
    vault: Vault, db_engine: Engine
) -> None:
    for _ in range(RACE_ROUNDS):
        document_id = vault.add()
        vault.trash(document_id)
        request = vault.request(name="Lease again.txt")
        deleted, imported = race(
            lambda: purge_document(vault.engine, vault.store, document_id),  # noqa: B023
            lambda: vault.importer(db_engine).import_file(request),  # noqa: B023
        )
        assert not isinstance(deleted, Exception), deleted
        assert not isinstance(imported, Exception), imported
        every_recorded_blob_has_its_file(vault)
        with vault.engine.connect() as connection:
            live: list[str] = list(
                connection.execute(select(documents.c.id).where(documents.c.state != "trashed"))
                .scalars()
                .all()
            )
        assert len(live) == 1  # the import's document, new or lifted out of the trash
        assert vault.store.find(SHA) is not None
        assert vault.store.verify(SHA)
        # Leave the vault empty for the next round.
        vault.trash(live[0])
        assert isinstance(purge_document(vault.engine, vault.store, live[0]), Purged)
        assert vault.store.find(SHA) is None


@pytest.fixture
def db_engine(vault: Vault, vault_dir: Path) -> Any:
    """A second engine on the same database, as a second thread of the server would have."""
    engine = create_vault_engine(vault_dir / "vault.db")
    yield engine
    engine.dispose()


# --- the write lock ----------------------------------------------------------------------------


def test_a_write_transaction_holds_the_lock_from_its_start(vault: Vault, vault_dir: Path) -> None:
    other = sqlite3.connect(vault_dir / "vault.db", timeout=0)
    try:
        with write_transaction(vault.engine) as connection:
            connection.execute(select(func.count()).select_from(documents))  # a read only
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")  # free again once it ended
        other.rollback()
    finally:
        other.close()


def test_a_write_transaction_rolls_back_when_its_block_raises(vault: Vault) -> None:
    document_id = vault.add()

    def rename_then_fail() -> None:
        with write_transaction(vault.engine) as connection:
            connection.execute(
                update(import_sources)
                .where(import_sources.c.document_id == document_id)
                .values(original_filename="changed.txt")
            )
            raise RuntimeError

    with pytest.raises(RuntimeError):
        rename_then_fail()
    with vault.engine.connect() as connection:
        names: list[str] = list(
            connection.execute(select(import_sources.c.original_filename)).scalars()
        )
    assert names == ["Patient-Lease.txt"]


def test_the_blob_step_decides_under_the_write_lock(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, vault_dir: Path
) -> None:
    """While the file is being removed, no other connection can write (so no import can record
    the hash in between)."""
    document_id = vault.add()
    vault.trash(document_id)
    delete_permanently(vault.engine, document_id)
    original = vault.store.delete
    blocked: list[bool] = []

    def delete_and_probe(sha256: str) -> bool:
        other = sqlite3.connect(vault_dir / "vault.db", timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            blocked.append(True)
        else:
            other.rollback()
            blocked.append(False)
        finally:
            other.close()
        return original(sha256)

    monkeypatch.setattr(vault.store, "delete", delete_and_probe)
    assert remove_unused_blob(vault.engine, vault.store, SHA)
    assert blocked == [True]
