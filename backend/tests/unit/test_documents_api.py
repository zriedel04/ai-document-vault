"""The document list: pages, sorting, the trash, names from the first import, and access."""

from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, update

from vault.api.documents import State
from vault.core.config import Settings
from vault.db import ids
from vault.db.documents import ListQuery, _utc
from vault.db.schema import DOCUMENT_STATES, TRASHED, documents, import_sources
from vault.ingest.importer import Importer, ImportRequest, Outcome
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

    def add(self, name: str, body: str) -> str:
        self.now += timedelta(minutes=1)
        path = self.folder / f"incoming-{ids.new_ulid()}.txt"
        path.write_text(body, encoding="utf-8")
        result = self.importer.import_file(
            ImportRequest(
                path=path, original_filename=name, method="upload", batch_id=ids.new_ulid()
            )
        )
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
