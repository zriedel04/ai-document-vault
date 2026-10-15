"""Importing a file: validation, the blob, the rows, the first job, duplicates and races."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, update

from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.db.schema import blobs, document_versions, documents, import_sources, jobs
from vault.ingest.importer import FIRST_STAGE, Importer, ImportRequest, Outcome
from vault.ingest.validate import Limits, Reason
from vault.jobs.queue import JobQueue
from vault.storage.blobs import BlobStore

NOW = datetime(2026, 10, 15, 12, 0, 0)
TEXT = b"Course syllabus\nWeek 1: introduction\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BINARY = b"\x00\x01\x02\x03" * 64


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "vault.db"
    upgrade_to_latest(path)
    return path


@pytest.fixture
def engine(db_file: Path) -> Iterator[Engine]:
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    root = tmp_path / "blobs"
    root.mkdir()
    return BlobStore(root)


@pytest.fixture
def importer(engine: Engine, store: BlobStore) -> Importer:
    return Importer(engine, store, clock=lambda: NOW)


@pytest.fixture
def incoming(tmp_path: Path) -> Path:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return folder


def write(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def request(path: Path, **changes: Any) -> ImportRequest:
    values: dict[str, Any] = {
        "path": path,
        "original_filename": path.name,
        "method": "upload",
        "batch_id": ids.new_ulid(),
    }
    return ImportRequest(**(values | changes))


def rows(engine: Engine, table: Any) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(select(table)).all())


# --- a new file --------------------------------------------------------------------------------


def test_a_new_file_becomes_a_document_with_its_first_job(
    engine: Engine, importer: Importer, store: BlobStore, incoming: Path
) -> None:
    path = write(incoming, "Syllabus.txt", TEXT)
    result = importer.import_file(
        request(
            path,
            original_path=r"C:\Users\Owner\School\CSE485\Syllabus.txt",
            folder_segments=["School", "CSE485"],
            original_mtime=NOW,
            method="folder",
        )
    )
    assert result.outcome is Outcome.IMPORTED
    assert result.reason is None
    (document,) = rows(engine, documents)
    (version,) = rows(engine, document_versions)
    (source,) = rows(engine, import_sources)
    (blob,) = rows(engine, blobs)
    (job,) = rows(engine, jobs)
    assert document.id == result.document_id
    assert document.state == "processing"
    assert document.current_version_id == version.id
    assert (version.version_no, version.origin, version.blob_sha256) == (1, "import", blob.sha256)
    assert (blob.ext, blob.mime, blob.size) == ("txt", "text/plain", len(TEXT))
    assert store.verify(blob.sha256)
    assert source.original_filename == "Syllabus.txt"
    assert source.original_path == r"C:\Users\Owner\School\CSE485\Syllabus.txt"
    assert json.loads(source.folder_segments) == ["School", "CSE485"]
    assert source.method == "folder"
    assert (job.stage, job.status, job.input_hash) == (FIRST_STAGE, "queued", blob.sha256)
    assert job.document_id == document.id


def test_the_extension_is_stored_in_its_canonical_form(
    engine: Engine, importer: Importer, incoming: Path
) -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    result = importer.import_file(request(write(incoming, "photo.JPEG", jpeg)))
    assert result.outcome is Outcome.IMPORTED
    (blob,) = rows(engine, blobs)
    assert (blob.ext, blob.mime) == ("jpg", "image/jpeg")


def test_names_outside_ascii_are_kept(engine: Engine, importer: Importer, incoming: Path) -> None:
    name = "R" + chr(0xE9) + "sum" + chr(0xE9) + " " + chr(0x4E2D) + ".txt"
    folders = ["Jos" + chr(0xE9), chr(0x1F4C1)]
    importer.import_file(request(write(incoming, name, TEXT), folder_segments=folders))
    (source,) = rows(engine, import_sources)
    assert source.original_filename == name
    assert json.loads(source.folder_segments) == folders


# --- the same bytes again ----------------------------------------------------------------------


def test_the_same_bytes_again_add_a_source_not_a_document(
    engine: Engine, importer: Importer, incoming: Path
) -> None:
    first = importer.import_file(request(write(incoming, "a.txt", TEXT)))
    second = importer.import_file(request(write(incoming, "copy of a.md", TEXT), method="folder"))
    assert second.outcome is Outcome.DUPLICATE
    assert second.document_id == first.document_id
    assert len(rows(engine, documents)) == 1
    assert len(rows(engine, import_sources)) == 2
    assert len(rows(engine, jobs)) == 1  # no second job for the same work
    (blob,) = rows(engine, blobs)
    assert blob.ext == "txt"  # stored once, under the name it first had


def test_a_trashed_document_imported_again_leaves_the_trash(
    engine: Engine, importer: Importer, incoming: Path
) -> None:
    first = importer.import_file(request(write(incoming, "a.txt", TEXT)))
    with engine.begin() as connection:
        connection.execute(update(documents).values(trashed_at=NOW))
    again = importer.import_file(request(write(incoming, "b.txt", TEXT)))
    assert again.outcome is Outcome.DUPLICATE
    assert again.document_id == first.document_id
    (document,) = rows(engine, documents)
    assert document.trashed_at is None


def test_other_bytes_make_another_document(
    engine: Engine, importer: Importer, incoming: Path
) -> None:
    importer.import_file(request(write(incoming, "a.txt", TEXT)))
    result = importer.import_file(request(write(incoming, "b.txt", TEXT + b"more\n")))
    assert result.outcome is Outcome.IMPORTED
    assert len(rows(engine, documents)) == 2


def test_imports_of_the_same_bytes_at_once_make_one_document(
    db_file: Path, engine: Engine, store: BlobStore, incoming: Path
) -> None:
    paths = [write(incoming, f"copy{n}.txt", TEXT) for n in range(8)]
    results: list[Outcome] = []
    lock = threading.Lock()
    start = threading.Barrier(len(paths))

    def run(path: Path) -> None:
        own = create_vault_engine(db_file)
        try:
            start.wait()
            result = Importer(own, store).import_file(request(path))
            with lock:
                results.append(result.outcome)
        finally:
            own.dispose()

    threads = [threading.Thread(target=run, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == sorted([Outcome.IMPORTED] + [Outcome.DUPLICATE] * 7)
    assert len(rows(engine, documents)) == 1
    assert len(rows(engine, import_sources)) == 8
    assert len(rows(engine, jobs)) == 1


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        ("scan.pdf", PNG, Reason.CONTENT_MISMATCH),
        ("notes.txt", b"", Reason.EMPTY),
        ("program.exe", BINARY, Reason.UNSUPPORTED_EXTENSION),
        ("notes.txt", BINARY, Reason.CONTENT_MISMATCH),
    ],
)
def test_a_file_that_is_not_acceptable_is_refused_and_leaves_nothing(
    engine: Engine,
    importer: Importer,
    store: BlobStore,
    incoming: Path,
    name: str,
    data: bytes,
    reason: Reason,
) -> None:
    result = importer.import_file(request(write(incoming, name, data)))
    assert (result.outcome, result.reason, result.document_id) == (Outcome.REFUSED, reason, None)
    for table in (blobs, documents, import_sources, jobs):
        assert rows(engine, table) == []
    assert store.scan().blobs == ()


def test_a_missing_file_is_refused(importer: Importer, incoming: Path) -> None:
    result = importer.import_file(request(incoming / "gone.txt"))
    assert (result.outcome, result.reason) == (Outcome.REFUSED, Reason.NOT_A_FILE)


def test_a_file_over_the_limit_is_refused(engine: Engine, store: BlobStore, incoming: Path) -> None:
    small = Importer(engine, store, limits=Limits(max_bytes=10))
    result = small.import_file(request(write(incoming, "big.txt", TEXT)))
    assert (result.outcome, result.reason) == (Outcome.REFUSED, Reason.TOO_LARGE)
    assert store.scan().blobs == ()


class SwappingStore(BlobStore):
    """Replaces the source with other bytes just before copying it, as another program could
    between the first check and the copy."""

    def __init__(self, root: Path, replacement: bytes) -> None:
        super().__init__(root)
        self.replacement = replacement

    def put_file(
        self, source: Path, extension: str | None = None, *, max_bytes: int | None = None
    ) -> Any:
        source.write_bytes(self.replacement)
        return super().put_file(source, extension, max_bytes=max_bytes)


def test_a_file_that_changes_after_the_check_is_judged_by_what_was_stored(
    engine: Engine, store: BlobStore, incoming: Path
) -> None:
    swapping = SwappingStore(store.root, BINARY)
    result = Importer(engine, swapping).import_file(request(write(incoming, "a.txt", TEXT)))
    assert (result.outcome, result.reason) == (Outcome.REFUSED, Reason.CONTENT_MISMATCH)
    assert store.scan().blobs == ()  # the blob it created is gone again
    for table in (blobs, documents, import_sources, jobs):
        assert rows(engine, table) == []


def test_a_refused_blob_that_was_already_stored_is_left_alone(
    engine: Engine, store: BlobStore, incoming: Path
) -> None:
    stored = store.put_file(write(incoming, "seed.txt", BINARY), "txt")
    swapping = SwappingStore(store.root, BINARY)
    result = Importer(engine, swapping).import_file(request(write(incoming, "a.txt", TEXT)))
    assert result.outcome is Outcome.REFUSED
    assert store.exists(stored.sha256)


@pytest.mark.parametrize(
    "change",
    [
        {"method": "email"},
        {"batch_id": "batch-1"},
        {"batch_id": ids.new_ulid().lower()},
        {"original_filename": ""},
        {"folder_segments": "School/CSE485"},
        {"folder_segments": ["School", 485]},
    ],
)
def test_a_malformed_request_is_an_error(
    importer: Importer, incoming: Path, change: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match=r"method|batch_id|original_filename|folder_segments"):
        importer.import_file(request(write(incoming, "a.txt", TEXT), **change))


# --- the job is part of the import's transaction -------------------------------------------------


def test_a_job_queued_in_a_transaction_that_fails_is_not_kept(engine: Engine) -> None:
    document_id = ids.new_ulid()
    queue = JobQueue(engine)

    class StopError(Exception):
        pass

    def write_then_fail() -> None:
        with engine.begin() as connection:
            connection.execute(
                documents.insert().values(
                    id=document_id, state="processing", local_only=False, created_at=NOW
                )
            )
            queue.enqueue(document_id, "extract", "ab" * 32, connection=connection)
            raise StopError

    with pytest.raises(StopError):
        write_then_fail()
    assert rows(engine, jobs) == []
    assert rows(engine, documents) == []
