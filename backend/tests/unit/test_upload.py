"""POST /api/imports/upload: one file as the request body, its name and batch in headers."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from vault.api import imports
from vault.api.imports import parse_length, parse_mtime
from vault.core.config import Settings
from vault.core.security import current_user
from vault.db import ids
from vault.db.schema import blobs, documents, import_sources, jobs
from vault.ingest.importer import Importer, ImportRequest
from vault.ingest.validate import Limits
from vault.main import create_app
from vault.storage.blobs import TEMP_FOLDER, BlobBusyError, BlobStore

from ..conftest import LOCAL

URL = "/api/imports/upload"
TEXT = b"Lecture notes, week 3: indexes and query plans.\n"
NAME = "Re" + chr(0x301) + "sume" + chr(0xE9) + " " + chr(0x4E2D) + ".txt"  # NFD, NFC, CJK


def send(client: TestClient, body: Any, name: str | None = NAME, **headers: str) -> Any:
    sent = {"Content-Type": "application/octet-stream"} | headers
    if name is not None:
        sent["X-Vault-Filename"] = quote(name, safe="")
    return client.post(URL, content=body, headers=sent)


def rows(engine: Engine, table: Any) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(select(table)).all())


def staged(app: FastAPI) -> list[Path]:
    folder = Path(app.state.blobs.root) / TEMP_FOLDER
    return (
        [entry for entry in folder.iterdir() if ".incoming" in entry.name]
        if folder.exists()
        else []
    )


@pytest.fixture
def engine(app: FastAPI) -> Engine:
    engine: Engine = app.state.engine
    return engine


def small(app: FastAPI, max_bytes: int) -> None:
    store: BlobStore = app.state.blobs
    app.state.importer = Importer(app.state.engine, store, limits=Limits(max_bytes=max_bytes))


# --- what an upload does -------------------------------------------------------------------------


def test_an_upload_becomes_a_document_with_its_source_and_first_job(
    app: FastAPI, client: TestClient, engine: Engine
) -> None:
    response = send(client, TEXT, **{"X-Vault-Last-Modified": "1760529600123"})
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "imported"
    assert ids.is_ulid(body["batch_id"])
    assert body["reason"] is None
    (document,) = rows(engine, documents)
    assert document.id == body["document_id"]
    (source,) = rows(engine, import_sources)
    assert source.original_filename == NAME  # as typed, not normalised
    assert source.original_path is None
    assert source.folder_segments == "[]"
    assert source.method == "upload"
    assert source.import_batch_id == body["batch_id"]
    assert source.original_mtime.isoformat() == "2025-10-15T12:00:00.123000"
    assert len(rows(engine, jobs)) == 1
    assert staged(app) == []


def test_the_same_bytes_again_in_the_same_batch_are_a_duplicate(
    client: TestClient, engine: Engine
) -> None:
    first = send(client, TEXT).json()
    again = send(client, TEXT, "copy.txt", **{"X-Vault-Batch": first["batch_id"]}).json()
    assert again["outcome"] == "duplicate"
    assert again["document_id"] == first["document_id"]
    assert again["batch_id"] == first["batch_id"]
    assert len(rows(engine, documents)) == 1
    assert len(rows(engine, import_sources)) == 2


def test_a_file_whose_bytes_are_not_what_its_name_says_is_refused(
    app: FastAPI, client: TestClient, engine: Engine
) -> None:
    body = send(client, TEXT, "notes.pdf").json()
    assert body["outcome"] == "refused"
    assert body["reason"] == "content_mismatch"
    assert body["document_id"] is None
    assert rows(engine, blobs) == []
    assert staged(app) == []


@pytest.mark.parametrize("name", ["notes.exe", "notes", "notes.", ".txt.exe"])
def test_an_extension_the_vault_does_not_take_is_refused_unread(
    app: FastAPI, client: TestClient, engine: Engine, name: str
) -> None:
    body = send(client, TEXT, name).json()
    assert (body["outcome"], body["reason"]) == ("refused", "unsupported_extension")
    assert rows(engine, blobs) == []
    assert staged(app) == []


def test_an_empty_file_is_refused(client: TestClient) -> None:
    assert send(client, b"").json()["reason"] == "empty"


# --- size --------------------------------------------------------------------------------------


def test_a_declared_length_over_the_limit_is_refused_before_reading(
    app: FastAPI, client: TestClient, engine: Engine
) -> None:
    small(app, 10)
    response = send(client, TEXT)
    assert response.status_code == 413
    assert rows(engine, blobs) == []
    assert staged(app) == []


def test_a_streamed_body_over_the_limit_is_cut_off_and_removed(
    app: FastAPI, client: TestClient, engine: Engine
) -> None:
    """Without a Content-Length (a chunked body) the limit is applied while writing."""
    small(app, 10)

    def chunks() -> Iterator[bytes]:
        yield TEXT[:8]
        yield TEXT[8:]

    response = send(client, chunks())
    assert response.status_code == 413
    assert rows(engine, blobs) == []
    assert staged(app) == []


def test_a_body_of_exactly_the_limit_is_taken(app: FastAPI, client: TestClient) -> None:
    small(app, len(TEXT))
    assert send(client, TEXT).json()["outcome"] == "imported"


# --- malformed requests --------------------------------------------------------------------------

PRIVATE = "Diagnosis-Patient"


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({}, "X-Vault-Filename is required"),
        ({"X-Vault-Filename": ""}, "X-Vault-Filename is required"),
        ({"X-Vault-Filename": f"{PRIVATE}%FF.txt"}, "not percent-encoded UTF-8"),
        ({"X-Vault-Filename": f"{PRIVATE}%ZZ.txt"}, "not percent-encoded UTF-8"),
        ({"X-Vault-Filename": f"{PRIVATE}%.txt"}, "not percent-encoded UTF-8"),
        ({"X-Vault-Filename": f"{PRIVATE}%4.txt"}, "not percent-encoded UTF-8"),
        ({"X-Vault-Filename": f"{PRIVATE}.txt%"}, "not percent-encoded UTF-8"),
        ({"X-Vault-Filename": quote(f"School/{PRIVATE}.txt", safe="")}, "not a file name"),
        ({"X-Vault-Filename": quote(f"School\\{PRIVATE}.txt", safe="")}, "not a file name"),
        ({"X-Vault-Filename": ".."}, "not a file name"),
        ({"X-Vault-Filename": "."}, "not a file name"),
        ({"X-Vault-Filename": f"{PRIVATE}%0A.txt"}, "not a file name"),
        ({"X-Vault-Filename": f"{PRIVATE}%00.txt"}, "not a file name"),
        ({"X-Vault-Filename": f"{PRIVATE}%7F.txt"}, "not a file name"),
        ({"X-Vault-Filename": "a" * 252 + ".txt"}, "not a file name"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Batch": PRIVATE}, "not a ULID"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Batch": ids.new_ulid().lower()}, "not a ULID"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Last-Modified": "-1"}, "milliseconds"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Last-Modified": "1e3"}, "milliseconds"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Last-Modified": "1.5"}, "milliseconds"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Last-Modified": "9" * 20}, "milliseconds"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Last-Modified": PRIVATE}, "milliseconds"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Last-Modified": "1" * 5000}, "milliseconds"),
        ({"X-Vault-Filename": "a.txt", "X-Vault-Last-Modified": "0" * 5000}, "milliseconds"),
    ],
)
def test_a_malformed_request_is_a_400_in_fixed_words(
    app: FastAPI, client: TestClient, engine: Engine, headers: dict[str, str], message: str
) -> None:
    response = send(client, TEXT, None, **headers)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert PRIVATE not in response.text
    assert rows(engine, blobs) == []
    assert staged(app) == []


def test_digits_from_another_script_are_not_a_time() -> None:
    """str.isdigit() accepts Arabic-Indic digits, and int() reads them; only ASCII is a time."""
    with pytest.raises(HTTPException) as refused:
        parse_mtime(chr(0x661) + chr(0x662))
    assert refused.value.status_code == 400


def test_a_failed_import_still_removes_the_staged_file(app: FastAPI, client: TestClient) -> None:
    class Broken(Importer):
        def import_file(self, request: ImportRequest) -> Any:
            assert request.path.is_file()
            raise BlobBusyError("busy")

    app.state.importer = Broken(app.state.engine, app.state.blobs)
    with pytest.raises(BlobBusyError):
        send(client, TEXT)
    assert staged(app) == []


def test_a_name_of_the_longest_length_is_taken(client: TestClient) -> None:
    assert send(client, TEXT, "a" * 251 + ".txt").json()["outcome"] == "imported"


def test_a_time_of_zero_is_1970(client: TestClient, engine: Engine) -> None:
    send(client, TEXT, **{"X-Vault-Last-Modified": "0"})
    (source,) = rows(engine, import_sources)
    assert source.original_mtime.isoformat() == "1970-01-01T00:00:00"


# --- who may upload ------------------------------------------------------------------------------


def test_an_upload_needs_the_token(anonymous: TestClient, engine: Engine) -> None:
    response = anonymous.post(
        URL, content=TEXT, headers={"X-Vault-Filename": "a.txt", "Origin": LOCAL}
    )
    assert response.status_code == 401
    assert rows(engine, blobs) == []


def test_an_upload_from_another_origin_is_refused(token: str, app: FastAPI) -> None:
    other = TestClient(
        app,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {token}", "Origin": "http://evil.example"},
    )
    assert send(other, TEXT).status_code == 403


def test_without_a_vault_there_is_nothing_to_upload_into() -> None:
    demo = create_app(Settings())
    client = TestClient(
        demo,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {demo.state.token}", "Origin": LOCAL},
    )
    response = send(client, TEXT)
    assert response.status_code == 503


# --- nothing private in the logs -----------------------------------------------------------------


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_the_file_name_never_reaches_a_log(client: TestClient) -> None:
    root = logging.getLogger()
    collector = Collect()
    root.addHandler(collector)
    level = root.level
    root.setLevel(logging.DEBUG)
    try:
        name = f"{PRIVATE}.txt"
        send(client, TEXT, name)
        send(client, TEXT, f"{PRIVATE}.pdf")
        send(client, TEXT, None, **{"X-Vault-Filename": f"{PRIVATE}%FF.txt"})
    finally:
        root.removeHandler(collector)
        root.setLevel(level)
    for record in collector.records:
        text = f"{record.getMessage()} {record.__dict__}"
        assert PRIVATE not in text


def test_a_content_length_of_thousands_of_digits_is_too_large_not_an_error() -> None:
    """int() refuses more than 4,300 digits with a ValueError of its own."""
    with pytest.raises(HTTPException) as refused:
        parse_length("9" * 5000, 100)
    assert refused.value.status_code == 413


@pytest.mark.parametrize("value", ["-1", "1e3", chr(0x661)])
def test_a_content_length_that_is_not_a_number_is_a_400(value: str) -> None:
    with pytest.raises(HTTPException) as refused:
        parse_length(value, 100)
    assert refused.value.status_code == 400


@pytest.mark.parametrize(
    ("millis", "stored"),
    [
        ("32535216000000", "3001-01-01T00:00:00"),  # past the Windows C library's range
        (str(imports.MAX_MTIME_MS), "9999-12-31T23:59:59.999000"),
    ],
)
def test_a_time_far_in_the_future_is_kept_on_every_system(
    client: TestClient, engine: Engine, millis: str, stored: str
) -> None:
    """Codex #32: datetime.fromtimestamp fails on Windows after the year 3000."""
    assert send(client, TEXT, **{"X-Vault-Last-Modified": millis}).status_code == 200
    (source,) = rows(engine, import_sources)
    assert source.original_mtime.isoformat() == stored


def test_one_millisecond_past_the_last_time_is_a_400(client: TestClient) -> None:
    late = str(imports.MAX_MTIME_MS + 1)
    assert send(client, TEXT, **{"X-Vault-Last-Modified": late}).status_code == 400


def off_the_loop(calls: list[str], name: str, function: Any) -> Any:
    """Wrap a disk call so that it records whether it ran outside the event loop's thread."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(name)  # no loop in this thread: a worker thread
        else:
            calls.append(f"{name} ON THE LOOP")
        return function(*args, **kwargs)

    return wrapper


def test_disk_work_runs_outside_the_event_loop_in_bounded_writes(
    app: FastAPI, client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex #32: a large upload must not hold up the loop that answers other requests."""
    calls: list[str] = []
    monkeypatch.setattr(imports, "WRITE_CHUNK", 16)
    monkeypatch.setattr(imports, "_write", off_the_loop(calls, "write", imports._write))
    monkeypatch.setattr(imports, "_finish", off_the_loop(calls, "finish", imports._finish))
    store: BlobStore = app.state.blobs
    monkeypatch.setattr(store, "staging_path", off_the_loop(calls, "stage", store.staging_path))

    def chunks() -> Iterator[bytes]:
        for start in range(0, len(TEXT), 5):
            yield TEXT[start : start + 5]

    assert send(client, chunks()).json()["outcome"] == "imported"
    assert not [call for call in calls if "LOOP" in call]
    assert "write" in calls
    assert calls[0] == "stage"
    assert calls[-1] == "finish"
    (blob,) = rows(engine, blobs)
    assert blob.size == len(TEXT)  # every part arrived, in order (the hash is checked on import)


def test_no_more_than_one_write_chunk_waits_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test client sends a body as one part, so the buffering is driven directly."""
    monkeypatch.setattr(imports, "WRITE_CHUNK", 16)
    written: list[int] = []
    real_write = imports._write

    def record(out: Any, data: bytearray) -> None:
        written.append(len(data))
        real_write(out, data)

    monkeypatch.setattr(imports, "_write", record)

    class Parts:
        async def stream(self) -> Any:
            for start in range(0, len(TEXT), 5):
                yield TEXT[start : start + 5]

    target = tmp_path / "staged.txt"
    assert asyncio.run(imports._receive(Parts(), target, 100))  # type: ignore[arg-type]
    assert target.read_bytes() == TEXT
    assert written == [16, 16, 16]  # parts of 5 fill the buffer to exactly one chunk each time
    assert sum(written) == len(TEXT)


def test_a_part_larger_than_a_chunk_is_written_in_chunk_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex #32: one large part from the server must not be buffered (or copied) whole."""
    monkeypatch.setattr(imports, "WRITE_CHUNK", 16)
    written: list[int] = []
    real_write = imports._write

    def record(out: Any, data: bytearray) -> None:
        written.append(len(data))
        real_write(out, data)

    monkeypatch.setattr(imports, "_write", record)
    body = bytes(range(256)) * 2  # 512 bytes, 32 chunks

    class Parts:
        async def stream(self) -> Any:
            yield body[:3]  # a small part first, so the large one does not start on a boundary
            yield body[3:]

    target = tmp_path / "staged.bin"
    assert asyncio.run(imports._receive(Parts(), target, 1000))  # type: ignore[arg-type]
    assert target.read_bytes() == body
    assert written == [16] * 32


def test_no_more_uploads_are_staged_or_imported_at_once_than_there_are_slots(
    app: FastAPI, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex #32: waiting uploads must not fill the shared worker pool the other routes use, with
    their imports or their staging writes, nor pile up staged files while imports run."""
    importer: Importer = app.state.importer
    real_import = importer.import_file
    lock = threading.Lock()
    running = 0
    most = 0
    release = threading.Event()

    def slow_import(request: ImportRequest) -> Any:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        release.wait(timeout=10)
        try:
            return real_import(request)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(importer, "import_file", slow_import)
    uploads = imports.UPLOAD_SLOTS + 2
    headers = {"Authorization": f"Bearer {token}", "Origin": LOCAL}
    with (
        TestClient(app, base_url=LOCAL, headers=headers) as shared,  # one event loop for all
        ThreadPoolExecutor(uploads) as pool,
    ):
        futures = [
            pool.submit(send, shared, TEXT + str(n).encode(), f"note-{n}.txt")
            for n in range(uploads)
        ]
        deadline = time.monotonic() + 10
        while running < imports.UPLOAD_SLOTS and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # time for a third import to start, if nothing stopped it
        assert running == imports.UPLOAD_SLOTS
        assert len(staged(app)) == imports.UPLOAD_SLOTS  # the others have not been read yet
        assert shared.get("/api/health").status_code == 200  # the page is still answered
        release.set()
        results = [future.result(timeout=10).json()["outcome"] for future in futures]
    assert results == ["imported"] * uploads
    assert most == imports.UPLOAD_SLOTS
    assert staged(app) == []


def test_the_route_asks_for_the_current_user(
    app: FastAPI, client: TestClient, engine: Engine
) -> None:
    """Codex #32: like every signed-in route, so that real accounts later cover it too. A
    current_user that refuses must stop the upload."""

    def nobody() -> None:
        raise HTTPException(status_code=401, detail="no user")

    app.dependency_overrides[current_user] = nobody
    try:
        assert send(client, TEXT).status_code == 401
    finally:
        app.dependency_overrides.clear()
    assert rows(engine, blobs) == []


def test_a_name_with_latin_1_bytes_in_the_header_is_refused() -> None:
    """A header can carry Latin-1; encodeURIComponent writes ASCII only."""
    with pytest.raises(HTTPException) as refused:
        imports.parse_filename("R" + chr(0xE9) + "sum" + chr(0xE9) + ".txt")
    assert refused.value.status_code == 400


def test_an_escaped_percent_sign_is_kept_in_the_name(client: TestClient, engine: Engine) -> None:
    send(client, TEXT, "100% done.txt")
    (source,) = rows(engine, import_sources)
    assert source.original_filename == "100% done.txt"
