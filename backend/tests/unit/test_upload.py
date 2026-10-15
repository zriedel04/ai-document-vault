"""POST /api/imports/upload: one file as the request body, its name and batch in headers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from vault.api.imports import parse_length, parse_mtime
from vault.core.config import Settings
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


@pytest.mark.parametrize("value", ["-1", "1e3", chr(0x661)])
def test_a_content_length_that_is_not_a_number_is_a_400(value: str) -> None:
    with pytest.raises(HTTPException) as refused:
        parse_length(value, 100)
    assert refused.value.status_code == 400
