"""The type and state filters of the document list: alone, repeated, with the label filter and
the trash, across pages, and refusals that do not repeat what was sent (synthetic data only)."""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.test_documents_api import JPEG, PNG, Library, import_bytes, set_state
from tests.unit.test_labels_api import add_label, assign
from vault.api.documents import FilterState
from vault.api.search import Extension
from vault.db.documents import EXTENSIONS, FILTER_STATES, ListQuery, list_documents
from vault.db.schema import LIVE_STATES
from vault.ingest.validate import MIME_TYPES

URL = "/api/documents"
CANARY = "Patient-Diagnosis-Canary"
# A query string as the test client takes it, in order and with repeated names.
Params = list[tuple[str, str | int | float | bool | None]]


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


def listing(client: TestClient, params: Params) -> dict[str, Any]:
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def ids_of(body: dict[str, Any]) -> list[str]:
    return sorted(item["id"] for item in body["items"])


@pytest.fixture
def mixed(library: Library) -> dict[str, str]:
    """One document of each kind the test vault can import, by its name."""
    return {
        "notes.txt": library.add("notes.txt", "plain notes\n"),
        "readme.md": library.add("readme.md", "# Heading\n"),
        "scan.png": import_bytes(library, "scan.png", PNG),
        "photo.jpeg": import_bytes(library, "photo.jpeg", JPEG),
    }


def test_a_type_keeps_only_documents_of_that_type(
    client: TestClient, mixed: dict[str, str]
) -> None:
    body = listing(client, [("type", "txt")])
    assert ids_of(body) == [mixed["notes.txt"]]
    assert body["total"] == 1
    assert [item["extension"] for item in body["items"]] == ["txt"]


def test_a_repeated_type_keeps_documents_of_any_of_them(
    client: TestClient, mixed: dict[str, str]
) -> None:
    body = listing(client, [("type", "txt"), ("type", "png"), ("type", "txt")])
    assert ids_of(body) == sorted([mixed["notes.txt"], mixed["scan.png"]])
    assert body["total"] == 2


def test_the_type_is_the_canonical_extension(client: TestClient, mixed: dict[str, str]) -> None:
    # A .jpeg file is stored as jpg, so jpg finds it and jpeg is not a type at all.
    assert ids_of(listing(client, [("type", "jpg")])) == [mixed["photo.jpeg"]]
    assert client.get(URL, params={"type": "jpeg"}).status_code == 422


def test_no_type_keeps_every_type(client: TestClient, mixed: dict[str, str]) -> None:
    assert listing(client, [])["total"] == len(mixed)


def test_a_state_keeps_only_documents_in_that_state(
    client: TestClient, library: Library, mixed: dict[str, str]
) -> None:
    set_state(library, mixed["notes.txt"], "failed")
    set_state(library, mixed["readme.md"], "searchable")
    set_state(library, mixed["scan.png"], "ready")
    set_state(library, mixed["photo.jpeg"], "processing")
    for state, name in (
        ("failed", "notes.txt"),
        ("searchable", "readme.md"),
        ("ready", "scan.png"),
        ("processing", "photo.jpeg"),
    ):
        body = listing(client, [("state", state)])
        assert ids_of(body) == [mixed[name]], state
        assert body["total"] == 1
        assert [item["state"] for item in body["items"]] == [state]
    both = listing(client, [("state", "failed"), ("state", "ready")])
    assert ids_of(both) == sorted([mixed["notes.txt"], mixed["scan.png"]])


def test_in_the_trash_a_state_is_the_one_a_restore_brings_back(
    client: TestClient, library: Library, mixed: dict[str, str]
) -> None:
    set_state(library, mixed["notes.txt"], "failed")
    set_state(library, mixed["readme.md"], "searchable")
    library.trash(mixed["notes.txt"])
    library.trash(mixed["readme.md"])
    trash = listing(client, [("trash", "true"), ("state", "failed")])
    assert ids_of(trash) == [mixed["notes.txt"]]
    assert trash["total"] == 1
    assert [item["state"] for item in trash["items"]] == ["trashed"]
    # And the library does not list the failed document that is in the trash.
    assert listing(client, [("state", "failed")])["total"] == 0


def test_type_state_and_label_combine_and_total_counts_what_passes_them_all(
    client: TestClient, library: Library
) -> None:
    school = add_label(library.engine, "School")
    wanted: list[str] = []
    for number in range(7):
        failed_txt = library.add(f"failed-{number}.txt", f"failed text {number}\n")
        set_state(library, failed_txt, "failed")
        assign(library.engine, failed_txt, school)
        wanted.append(failed_txt)
        # Each of these misses one filter: the state, the type or the label.
        searchable = library.add(f"fine-{number}.txt", f"fine text {number}\n")
        set_state(library, searchable, "searchable")
        assign(library.engine, searchable, school)
        markdown = library.add(f"failed-{number}.md", f"# failed markdown {number}\n")
        set_state(library, markdown, "failed")
        assign(library.engine, markdown, school)
        unlabelled = library.add(f"other-{number}.txt", f"other text {number}\n")
        set_state(library, unlabelled, "failed")
    filters: Params = [("type", "txt"), ("state", "failed"), ("label", school)]
    seen: list[str] = []
    for offset in range(0, 9, 3):
        body = listing(client, [*filters, ("limit", "3"), ("offset", str(offset))])
        assert body["total"] == len(wanted)
        seen.extend(item["id"] for item in body["items"])
    assert sorted(seen) == sorted(wanted)
    past = listing(client, [*filters, ("limit", "3"), ("offset", "30")])
    assert (past["items"], past["total"]) == ([], len(wanted))


@pytest.mark.parametrize(
    ("name", "value", "detail"),
    [
        ("type", "exe", "unknown document type"),
        ("type", "PDF", "unknown document type"),
        ("type", "pdf\n", "unknown document type"),
        ("type", " pdf", "unknown document type"),
        ("type", "", "unknown document type"),
        ("type", CANARY, "unknown document type"),
        ("state", "trashed", "unknown document state"),
        ("state", "Failed", "unknown document state"),
        ("state", "failed\n", "unknown document state"),
        ("state", "", "unknown document state"),
        ("state", CANARY, "unknown document state"),
    ],
)
def test_an_unknown_value_is_a_422_that_does_not_repeat_it(
    client: TestClient, library: Library, name: str, value: str, detail: str
) -> None:
    library.add("notes.txt", "plain notes\n")
    # A known value beside it does not make the unknown one pass.
    known = "txt" if name == "type" else "processing"
    response = client.get(URL, params=[(name, known), (name, value)])
    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    if value.strip():
        assert value.strip() not in response.text


def test_the_filters_take_exactly_the_stored_extensions_and_live_states() -> None:
    assert set(typing.get_args(Extension)) == EXTENSIONS
    assert {extension for extension, _kind in MIME_TYPES} == EXTENSIONS
    assert typing.get_args(FilterState) == LIVE_STATES
    assert frozenset(LIVE_STATES) == FILTER_STATES


def test_the_query_object_refuses_an_unknown_type_or_state_with_fixed_text() -> None:
    with pytest.raises(ValueError, match=r"^unknown document type\Z"):
        ListQuery(extensions=("pdf", CANARY))
    with pytest.raises(ValueError, match=r"^unknown document state\Z"):
        ListQuery(states=("trashed",))


def test_the_repository_filters_like_the_route(library: Library) -> None:
    text = library.add("notes.txt", "plain notes\n")
    library.add("readme.md", "# Heading\n")
    set_state(library, text, "failed")
    page = list_documents(library.engine, ListQuery(extensions=("txt",), states=("failed",)))
    assert ([item.id for item in page.items], page.total) == ([text], 1)
