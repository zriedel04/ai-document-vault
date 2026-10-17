"""The document list: pages, sorting, the trash, names from the first import, and access."""

from __future__ import annotations

import hashlib
import typing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, event, func, select, update

from vault.api.documents import JobStatus, Method, Origin, Stage, State
from vault.core.config import Settings
from vault.db import ids
from vault.db.documents import ListQuery, _utc, get_document, list_documents
from vault.db.engine import read_snapshot
from vault.db.schema import (
    DOCUMENT_STATES,
    IMPORT_METHODS,
    JOB_STAGES,
    JOB_STATUSES,
    TRASHED,
    VERSION_ORIGINS,
    documents,
    import_sources,
)
from vault.ingest.importer import Importer, ImportRequest, Outcome
from vault.jobs.queue import JobQueue
from vault.main import create_app

from ..conftest import LOCAL

URL = "/api/documents"
START = datetime(2026, 10, 1, 9, 0, 0)


class Library:
    """Imports synthetic text files into the test vault, one minute apart."""

    def __init__(self, app: FastAPI, folder: Path) -> None:
        self.engine: Engine = app.state.engine
        self.folder = folder
        self.now = START
        self.importer = Importer(self.engine, app.state.blobs, clock=lambda: self.now)

    def add(self, name: str, body: str, **changes: Any) -> str:
        self.now += timedelta(minutes=1)
        path = self.folder / f"incoming-{ids.new_ulid()}.txt"
        path.write_bytes(body.encode("utf-8"))  # write_text would turn \n into \r\n on Windows
        values: dict[str, Any] = {
            "path": path,
            "original_filename": name,
            "method": "upload",
            "batch_id": ids.new_ulid(),
        }
        result = self.importer.import_file(ImportRequest(**(values | changes)))
        assert result.outcome in {Outcome.IMPORTED, Outcome.DUPLICATE}
        assert result.document_id is not None
        return result.document_id

    def trash(self, document_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(documents)
                .where(documents.c.id == document_id)
                .values(state=TRASHED, state_before_trash=documents.c.state, trashed_at=self.now)
            )


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


def page(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def names(body: dict[str, Any]) -> list[str]:
    return [item["name"] for item in body["items"]]


def test_an_empty_vault_lists_nothing(client: TestClient) -> None:
    assert page(client) == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_a_document_is_listed_with_its_name_type_size_and_time(
    client: TestClient, library: Library
) -> None:
    document_id = library.add("Syllabus.txt", "Week 1: introduction\n")
    [item] = page(client)["items"]
    assert item == {
        "id": document_id,
        "name": "Syllabus.txt",
        "extension": "txt",
        "mime": "text/plain",
        "size": len(b"Week 1: introduction\n"),
        "state": "processing",
        "added_at": "2026-10-01T09:01:00Z",
        "trashed_at": None,
        "sources": 1,
    }


def test_newest_first_by_default_and_oldest_first_on_request(
    client: TestClient, library: Library
) -> None:
    for number in range(3):
        library.add(f"note-{number}.txt", f"note {number}\n")
    assert names(page(client)) == ["note-2.txt", "note-1.txt", "note-0.txt"]
    assert names(page(client, order="asc")) == ["note-0.txt", "note-1.txt", "note-2.txt"]


def test_pages_cover_every_document_once_in_order(client: TestClient, library: Library) -> None:
    added = [library.add(f"note-{number:02}.txt", f"note {number}\n") for number in range(7)]
    seen = []
    for offset in range(0, 9, 3):
        body = page(client, limit=3, offset=offset, order="asc")
        assert body["total"] == 7
        assert body["limit"] == 3
        assert body["offset"] == offset
        seen += [item["id"] for item in body["items"]]
    assert seen == added


def test_a_page_past_the_end_is_empty_and_still_counts(
    client: TestClient, library: Library
) -> None:
    library.add("only.txt", "only\n")
    assert page(client, offset=5) == {"items": [], "total": 1, "limit": 50, "offset": 5}


def test_by_name_ignores_letter_case_and_breaks_ties_by_id(
    client: TestClient, library: Library
) -> None:
    library.add("banana.txt", "b\n")
    first_twin = library.add("Apple.txt", "a1\n")
    second_twin = library.add("Apple.txt", "a2\n")
    library.add("apricot.txt", "c\n")
    library.add("Cherry.txt", "d\n")  # before "banana" if upper case came first
    body = page(client, sort="name", order="asc")
    assert names(body) == ["Apple.txt", "Apple.txt", "apricot.txt", "banana.txt", "Cherry.txt"]
    assert [item["id"] for item in body["items"][:2]] == sorted([first_twin, second_twin])
    descending = page(client, sort="name")
    assert [item["id"] for item in descending["items"]] == [item["id"] for item in body["items"]][
        ::-1
    ]


def test_by_size(client: TestClient, library: Library) -> None:
    library.add("medium.txt", "x" * 20)
    library.add("small.txt", "x")
    library.add("large.txt", "x" * 300)
    assert names(page(client, sort="size")) == ["large.txt", "medium.txt", "small.txt"]
    assert names(page(client, sort="size", order="asc")) == [
        "small.txt",
        "medium.txt",
        "large.txt",
    ]


def test_the_trash_is_its_own_list(client: TestClient, library: Library) -> None:
    kept = library.add("kept.txt", "kept\n")
    thrown = library.add("thrown.txt", "thrown\n")
    library.trash(thrown)
    in_use = page(client)
    assert [item["id"] for item in in_use["items"]] == [kept]
    assert in_use["total"] == 1
    trash = page(client, trash=True)
    assert [item["id"] for item in trash["items"]] == [thrown]
    assert trash["items"][0]["state"] == "trashed"
    assert trash["items"][0]["trashed_at"] == "2026-10-01T09:02:00Z"


def test_the_name_is_the_first_import_and_every_import_is_counted(
    client: TestClient, library: Library
) -> None:
    document_id = library.add("Draft.txt", "same bytes\n")
    assert library.add("Draft (copy).txt", "same bytes\n") == document_id
    [item] = page(client)["items"]
    assert item["name"] == "Draft.txt"
    assert item["sources"] == 2


def test_the_first_import_wins_even_when_a_later_one_has_a_smaller_id(
    client: TestClient, library: Library
) -> None:
    """The order is by time first; the id only breaks a tie in time."""
    document_id = library.add("Original.txt", "same bytes\n")
    library.add("Later.txt", "same bytes\n")
    with library.engine.begin() as connection:
        connection.execute(
            update(import_sources)
            .where(import_sources.c.original_filename == "Later.txt")
            .values(id="00000000000000000000000000")
        )
    [item] = page(client)["items"]
    assert (item["id"], item["name"]) == (document_id, "Original.txt")


def test_a_document_without_an_import_source_is_still_listed(
    client: TestClient, library: Library
) -> None:
    """The importer never leaves one, but a hand edit could: the list shows it without a name
    instead of hiding it."""
    document_id = library.add("lost.txt", "lost\n")
    with library.engine.begin() as connection:
        connection.execute(delete(import_sources))
    [item] = page(client)["items"]
    assert (item["id"], item["name"], item["sources"]) == (document_id, None, 0)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 1_000_000_001},
        {"sort": "created_at"},
        {"order": "up"},
        {"trash": "maybe"},
    ],
)
def test_a_bad_query_is_refused(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get(URL, params=params).status_code == 422


def test_the_query_object_refuses_what_the_route_refuses() -> None:
    for limit, offset in ((0, 0), (101, 0), (1, -1)):
        with pytest.raises(ValueError, match="must"):
            ListQuery(limit=limit, offset=offset)


def test_the_list_needs_the_token(anonymous: TestClient) -> None:
    assert anonymous.get(URL).status_code == 401


def test_without_a_vault_there_is_no_list() -> None:
    app = create_app(Settings())
    client = TestClient(app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}"})
    assert client.get(URL).status_code == 503


def test_the_api_lists_every_document_state() -> None:
    assert typing.get_args(State) == DOCUMENT_STATES


def test_a_time_is_given_in_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert _utc(datetime(2026, 10, 1, 13, 0, tzinfo=plus_two)) == datetime(
        2026, 10, 1, 11, 0, tzinfo=UTC
    )
    assert _utc(datetime(2026, 10, 1, 11, 0)) == datetime(2026, 10, 1, 11, 0, tzinfo=UTC)


def test_a_page_past_the_end_and_its_total_come_from_one_snapshot(library: Library) -> None:
    """Codex #41: a commit between the empty page and the count must not change the total."""
    library.add("first.txt", "first\n")
    counted: list[str] = []

    def import_during_the_count(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        # The fallback count, after the page came back empty (the page holds a window count).
        if statement.lstrip().upper().startswith("SELECT COUNT") and not counted:
            counted.append(statement)
            library.add("second.txt", "second\n")  # commits on another connection

    event.listen(library.engine, "before_cursor_execute", import_during_the_count)
    try:
        page_ = list_documents(library.engine, ListQuery(offset=10))
    finally:
        event.remove(library.engine, "before_cursor_execute", import_during_the_count)
    assert counted  # the import really ran between the two reads
    assert (page_.items, page_.total) == ((), 1)
    assert list_documents(library.engine, ListQuery(offset=10)).total == 2


# --- one document ---------------------------------------------------------------------------------


def test_one_document_with_its_version_imports_and_jobs(
    client: TestClient, library: Library
) -> None:
    batch = ids.new_ulid()
    document_id = library.add(
        "Syllabus.txt",
        "Week 1: introduction\n",
        method="folder",
        batch_id=batch,
        original_path="C:/School/Term 1/Syllabus.txt",
        folder_segments=["School", "Term 1"],
        original_mtime=datetime(2026, 9, 1, 8, 30),
    )
    library.add("Syllabus (copy).txt", "Week 1: introduction\n")
    response = client.get(f"{URL}/{document_id}")
    assert response.status_code == 200
    body = response.json()
    listed = page(client)["items"][0]
    assert {key: body[key] for key in listed} == listed  # the detail holds the list item
    assert body["sha256"] == hashlib.sha256(b"Week 1: introduction\n").hexdigest()
    assert body["local_only"] is False
    assert body["version_no"] == 1
    assert body["version_origin"] == "import"
    assert ids.is_ulid(body["version_id"])
    first, second = body["import_sources"]
    assert first | {"id": None} == {
        "id": None,
        "filename": "Syllabus.txt",
        "original_path": "C:/School/Term 1/Syllabus.txt",
        "folders": ["School", "Term 1"],
        "original_mtime": "2026-09-01T08:30:00Z",
        "method": "folder",
        "batch_id": batch,
        "imported_at": "2026-10-01T09:01:00Z",
    }
    assert (second["filename"], second["original_path"], second["folders"]) == (
        "Syllabus (copy).txt",
        None,
        [],
    )
    assert body["jobs"] == [
        {
            "stage": "extract",
            "status": "queued",
            "attempts": 0,
            "max_attempts": body["jobs"][0]["max_attempts"],
            "last_error": None,
            "finished_at": None,
        }
    ]


def test_jobs_come_in_pipeline_order(client: TestClient, library: Library, app: FastAPI) -> None:
    document_id = library.add("scan.txt", "scan\n")
    queue = JobQueue(app.state.engine)
    for stage in ("embed", "chunk", "ocr"):
        queue.enqueue(document_id, stage, "0" * 64)
    stages = [job["stage"] for job in client.get(f"{URL}/{document_id}").json()["jobs"]]
    assert stages == ["extract", "ocr", "chunk", "embed"]


def test_a_document_in_the_trash_has_details_too(client: TestClient, library: Library) -> None:
    document_id = library.add("old.txt", "old\n")
    library.trash(document_id)
    body = client.get(f"{URL}/{document_id}").json()
    assert (body["state"], body["trashed_at"]) == ("trashed", "2026-10-01T09:01:00Z")


@pytest.mark.parametrize(
    "document_id", [None, "01J9ZZZZZZZZZZZZZZZZZZZZZZ", "Patient-Name", "a" * 26]
)
def test_an_unknown_document_is_a_404_that_does_not_repeat_the_id(
    client: TestClient, library: Library, document_id: str | None
) -> None:
    if document_id is None:
        document_id = ids.new_ulid()
    library.add("other.txt", "other\n")
    response = client.get(f"{URL}/{document_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such document"}


def test_the_details_need_the_token(anonymous: TestClient) -> None:
    assert anonymous.get(f"{URL}/{ids.new_ulid()}").status_code == 401


def test_without_a_vault_there_are_no_details() -> None:
    app = create_app(Settings())
    client = TestClient(app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}"})
    assert client.get(f"{URL}/{ids.new_ulid()}").status_code == 503


def test_folders_that_are_not_names_are_refused(library: Library) -> None:
    """The table only checks for a JSON list; a list of numbers is not a folder path."""
    document_id = library.add("odd.txt", "odd\n")
    with library.engine.begin() as connection:
        connection.execute(update(import_sources).values(folder_segments="[1, 2]"))
    with pytest.raises(ValueError, match="not a list of names"):
        get_document(library.engine, document_id)


def test_the_api_lists_every_origin_method_stage_and_job_status() -> None:
    assert typing.get_args(Origin) == VERSION_ORIGINS
    assert typing.get_args(Method) == IMPORT_METHODS
    assert typing.get_args(Stage) == JOB_STAGES
    assert typing.get_args(JobStatus) == JOB_STATUSES


def test_a_snapshot_does_not_see_a_commit_made_during_it(library: Library) -> None:
    """The driver opens no transaction for a read, so without the snapshot the second read would
    see the document added between the two."""
    library.add("first.txt", "first\n")
    with read_snapshot(library.engine) as connection:
        before = connection.execute(select(func.count()).select_from(documents)).scalar_one()
        library.add("second.txt", "second\n")
        after = connection.execute(select(func.count()).select_from(documents)).scalar_one()
    assert (before, after) == (1, 1)
    with library.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(documents)).scalar_one() == 2
