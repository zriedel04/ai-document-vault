"""The text view's endpoint: a document's extracted pages, a window at a time, and access."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, select, update

from tests.unit.test_documents_api import Library
from vault.api import documents as documents_api
from vault.core.config import Settings
from vault.db import ids
from vault.db.pages import (
    MAX_WINDOW,
    PageDataError,
    PageHeading,
    PageText,
    read_current_pages,
    replace_pages,
)
from vault.db.schema import document_versions, documents, pages
from vault.main import create_app

from ..conftest import LOCAL

URL = "/api/documents"
# Synthetic text with letters outside ASCII and a character outside the Basic Multilingual Plane,
# built from code points, so offsets in characters and in UTF-16 units differ.
UNICODE = "café — 履歴書 \U0001f4c4"
CANARY = "canary-page-text-xyzzy"


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


@pytest.fixture
def engine(app: FastAPI) -> Engine:
    result: Engine = app.state.engine
    return result


def current_version(engine: Engine, document_id: str) -> str:
    with engine.connect() as connection:
        version: str = connection.execute(
            select(documents.c.current_version_id).where(documents.c.id == document_id)
        ).scalar_one()
    return version


def plain_pages(count: int, text: str = "page {n}\n") -> list[PageText]:
    return [PageText(n, text.format(n=n)) for n in range(1, count + 1)]


def store(engine: Engine, document_id: str, pages_in: list[PageText]) -> None:
    replace_pages(engine, current_version(engine, document_id), pages_in, "text-1", "utf-8")


def window(client: TestClient, document_id: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"{URL}/{document_id}/pages", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def numbers(body: dict[str, Any]) -> list[int]:
    return [page["number"] for page in body["pages"]]


# --- what comes back -----------------------------------------------------------------------------


def test_a_document_not_extracted_yet_says_so_instead_of_a_404(
    client: TestClient, library: Library, engine: Engine
) -> None:
    document_id = library.add("notes.txt", "notes\n")
    assert window(client, document_id) == {
        "version_id": current_version(engine, document_id),
        "extracted": False,
        "extractor": None,
        "total": 0,
        "offset": 0,
        "limit": 20,
        "pages": [],
        "next_offset": None,
    }


def test_pages_come_back_with_their_numbers_text_and_headings(
    client: TestClient, library: Library, engine: Engine
) -> None:
    document_id = library.add("notes.md", "# notes\n")
    first = f"# {UNICODE}\nintro\n## Plan\nstep one\n"
    plan = first.index("## Plan")
    headings = (
        PageHeading(1, UNICODE, 0, len(UNICODE) + 2),
        PageHeading(2, "Plan", plan, plan + 7),
    )
    store(engine, document_id, [PageText(1, first, headings), PageText(2, "", ())])

    body = window(client, document_id)
    assert body["extracted"] is True
    assert body["extractor"] == "text-1"
    assert (body["total"], body["next_offset"]) == (2, None)
    assert body["pages"] == [
        {
            "number": 1,
            "text": first,
            "headings": [
                {"level": 1, "text": UNICODE, "offset": 0},
                {"level": 2, "text": "Plan", "offset": plan},
            ],
        },
        {"number": 2, "text": "", "headings": []},
    ]
    # The offset counts characters (code points), as Python does, so it finds the heading.
    for heading in body["pages"][0]["headings"]:
        assert first[heading["offset"] :].startswith("#")


def test_markup_in_the_text_comes_back_as_written_in_json(
    client: TestClient, library: Library, engine: Engine
) -> None:
    document_id = library.add("page.txt", "page\n")
    markup = '<script>alert("x")</script><img src=x onerror=alert(1)> &amp;'
    store(engine, document_id, [PageText(1, markup)])
    response = client.get(f"{URL}/{document_id}/pages")
    assert response.headers["content-type"] == "application/json"
    assert response.json()["pages"][0]["text"] == markup


def test_many_pages_come_a_window_at_a_time_and_each_page_once(
    client: TestClient, library: Library, engine: Engine
) -> None:
    document_id = library.add("long.txt", "long\n")
    store(engine, document_id, plain_pages(250))

    first = window(client, document_id)
    assert numbers(first) == list(range(1, 21))
    assert (first["total"], first["next_offset"]) == (250, 20)

    seen: list[int] = []
    offset: int | None = 0
    while offset is not None:
        body = window(client, document_id, offset=offset, limit=MAX_WINDOW)
        assert body["offset"] == offset
        seen += numbers(body)
        offset = body["next_offset"]
    assert seen == list(range(1, 251))


def test_a_window_past_the_end_is_empty_and_still_counts(
    client: TestClient, library: Library, engine: Engine
) -> None:
    document_id = library.add("short.txt", "short\n")
    store(engine, document_id, plain_pages(3))
    body = window(client, document_id, offset=3)
    assert (body["pages"], body["total"], body["next_offset"]) == ([], 3, None)
    assert window(client, document_id, offset=50)["pages"] == []


def test_a_window_stops_before_the_page_that_passes_the_character_budget(
    client: TestClient, library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents_api, "MAX_WINDOW_CHARS", 25)
    document_id = library.add("budget.txt", "budget\n")
    store(engine, document_id, [PageText(n, "x" * 10) for n in range(1, 6)])

    # 0, 10 and 20 characters come before pages 1, 2 and 3; page 4 would start at 30.
    body = window(client, document_id)
    assert (numbers(body), body["next_offset"]) == ([1, 2, 3], 3)
    body = window(client, document_id, offset=3)
    assert (numbers(body), body["next_offset"]) == ([4, 5], None)


def test_a_page_larger_than_the_budget_is_sent_whole_and_alone(
    client: TestClient, library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents_api, "MAX_WINDOW_CHARS", 25)
    document_id = library.add("big.txt", "big\n")
    big = UNICODE * 20
    store(engine, document_id, [PageText(1, big), PageText(2, "next")])
    body = window(client, document_id)
    assert [page["text"] for page in body["pages"]] == [big]
    assert body["next_offset"] == 1
    assert numbers(window(client, document_id, offset=1)) == [2]


def test_only_the_current_versions_pages_are_shown(
    client: TestClient, library: Library, engine: Engine
) -> None:
    document_id = library.add("draft.txt", "draft\n")
    old_version = current_version(engine, document_id)
    replace_pages(engine, old_version, [PageText(1, "old words")], "text-1", "utf-8")
    new_version = ids.new_ulid()
    with engine.begin() as connection:
        blob: str = connection.execute(
            select(document_versions.c.blob_sha256).where(document_versions.c.id == old_version)
        ).scalar_one()
        connection.execute(
            insert(document_versions).values(
                id=new_version,
                document_id=document_id,
                blob_sha256=blob,
                version_no=2,
                origin="user",
                created_at=datetime(2026, 10, 2, 9, 0, 0),
            )
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(current_version_id=new_version)
        )

    # The new version is not extracted yet: the old version's text is not shown in its place.
    body = window(client, document_id)
    assert (body["version_id"], body["extracted"], body["pages"]) == (new_version, False, [])

    replace_pages(engine, new_version, [PageText(1, "new words"), PageText(2, "")], "text-2", None)
    body = window(client, document_id)
    assert body["version_id"] == new_version
    assert body["extractor"] == "text-2"
    assert [page["text"] for page in body["pages"]] == ["new words", ""]


def test_a_document_in_the_trash_shows_its_text_like_its_details(
    client: TestClient, library: Library, engine: Engine
) -> None:
    document_id = library.add("old.txt", "old\n")
    store(engine, document_id, [PageText(1, "kept")])
    library.trash(document_id)
    assert client.get(f"{URL}/{document_id}").status_code == 200
    assert [page["text"] for page in window(client, document_id)["pages"]] == ["kept"]


# --- refusals ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document_id", [None, "01J9ZZZZZZZZZZZZZZZZZZZZZZ", "Patient-Name", "a" * 26]
)
def test_an_unknown_document_is_a_404_that_does_not_repeat_the_id(
    client: TestClient, library: Library, document_id: str | None
) -> None:
    if document_id is None:
        document_id = ids.new_ulid()
    library.add("other.txt", "other\n")
    response = client.get(f"{URL}/{document_id}/pages")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such document"}


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": MAX_WINDOW + 1},
        {"limit": "ten"},
        {"offset": -1},
        {"offset": 1_000_000_001},
        {"offset": "1.5"},
    ],
)
def test_a_bad_window_is_refused(
    client: TestClient, library: Library, params: dict[str, Any]
) -> None:
    document_id = library.add("window.txt", "window\n")
    assert client.get(f"{URL}/{document_id}/pages", params=params).status_code == 422


def test_the_repository_refuses_what_the_route_refuses(engine: Engine) -> None:
    for offset, limit, budget in [(0, 0, 1), (0, MAX_WINDOW + 1, 1), (-1, 1, 1), (0, 1, 0)]:
        with pytest.raises(ValueError, match="window"):
            read_current_pages(engine, ids.new_ulid(), offset, limit, budget)


def test_the_pages_need_the_token(anonymous: TestClient) -> None:
    assert anonymous.get(f"{URL}/{ids.new_ulid()}/pages").status_code == 401


def test_without_a_vault_there_are_no_pages() -> None:
    app = create_app(Settings())
    client = TestClient(app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}"})
    assert client.get(f"{URL}/{ids.new_ulid()}/pages").status_code == 503


def raw_page(version_id: str, page_no: int, **changes: Any) -> dict[str, Any]:
    text = changes.pop("text", CANARY)
    return {
        "version_id": version_id,
        "page_no": page_no,
        "text": text,
        "char_count": len(text),
        "headings": "[]",
        "extractor": "text-1",
        "encoding": "utf-8",
    } | changes


@pytest.mark.parametrize(
    ("rows", "params"),
    [
        # A gap after the window: found from the whole version, not only the pages sent.
        ([{"page_no": 1}, {"page_no": 2}, {"page_no": 4}], {"limit": 1}),
        ([{"page_no": 2}], {}),
        ([{"page_no": 1}, {"page_no": 2, "extractor": "text-2"}], {"limit": 1}),
        ([{"page_no": 1}, {"page_no": 2, "encoding": None}], {"offset": 1}),
        ([{"page_no": 1, "extractor": "text-1-2"}], {}),
        ([{"page_no": 1, "encoding": "utf8"}], {}),
        ([{"page_no": 1, "headings": f'[[1, "{CANARY}", 0, 999]]'}], {}),
        ([{"page_no": 1, "headings": "[[1]]"}], {}),
    ],
)
def test_damaged_pages_are_a_500_that_logs_no_text(
    client: TestClient,
    library: Library,
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
    rows: list[dict[str, Any]],
    params: dict[str, Any],
) -> None:
    document_id = library.add("Diagnosis-Patient.txt", "private\n")
    version_id = current_version(engine, document_id)
    with engine.begin() as connection:
        for row in rows:
            connection.execute(insert(pages).values(raw_page(version_id, **row)))

    caplog.set_level(logging.DEBUG)
    response = client.get(f"{URL}/{document_id}/pages", params=params)
    assert response.status_code == 500
    assert response.json() == {"detail": "the extracted text of this document is damaged"}
    assert "documents.pages_invalid" in caplog.text
    assert CANARY not in caplog.text
    assert "Diagnosis" not in caplog.text
    # The test client logs the address it asked for; the application's own records do not.
    own = [record for record in caplog.records if record.name.startswith("vault")]
    assert own
    assert not any(document_id in f"{record.getMessage()} {record.__dict__}" for record in own)


def test_the_repository_names_damage_by_page_number_only(library: Library, engine: Engine) -> None:
    document_id = library.add("damaged.txt", "damaged\n")
    version_id = current_version(engine, document_id)
    with engine.begin() as connection:
        connection.execute(insert(pages).values(raw_page(version_id, 1)))
        connection.execute(
            insert(pages).values(raw_page(version_id, 2, headings=f'[[1, "{CANARY}", 0, 99]]'))
        )
    with pytest.raises(PageDataError, match="Heading 1 of page 2") as caught:
        read_current_pages(engine, document_id, 1)
    assert CANARY not in str(caught.value)


def test_reading_the_pages_logs_no_text(
    client: TestClient, library: Library, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    document_id = library.add("quiet.txt", "quiet\n")
    store(engine, document_id, [PageText(1, f"# {CANARY}", (PageHeading(1, CANARY, 0, 24),))])
    caplog.set_level(logging.DEBUG)
    assert window(client, document_id)["pages"][0]["text"] == f"# {CANARY}"
    assert CANARY not in caplog.text
