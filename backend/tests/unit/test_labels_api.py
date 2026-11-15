"""Browsing by label: the label tree with its counts, and the label filter on the document list
and on keyword search (synthetic names and text only)."""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, insert, select, update

from tests.unit.test_documents_api import START, Library
from tests.unit.test_search import add as add_indexed
from vault.core.config import Settings
from vault.db import ids
from vault.db.documents import ListQuery, list_documents
from vault.db.labels import REJECTED, UnknownLabelError, label_tree
from vault.db.schema import LABEL_STATUSES, document_labels, documents, labels
from vault.main import create_app
from vault.retrieval.keyword import SearchQuery, search

from ..conftest import LOCAL

LABELS = "/api/labels"
DOCUMENTS = "/api/documents"
SEARCH = "/api/search"
CANARY = "Patient-Diagnosis-Canary"
# Names shaped like SQL, FTS5 and LIKE syntax: each must be stored and shown as written.
HOSTILE = (
    "'); DROP TABLE labels; --",
    '" OR 1=1 --',
    "%_ LIKE *",
    "<script>alert(1)</script>",
    "a AND b NEAR c",
)


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


def tree(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(LABELS)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def by_path(client: TestClient) -> dict[tuple[str, ...], dict[str, Any]]:
    return {tuple(item["path"]): item for item in tree(client)}


def listed(client: TestClient, label: str, **params: Any) -> list[str]:
    response = client.get(DOCUMENTS, params={"label": label, **params})
    assert response.status_code == 200, response.text
    return sorted(item["id"] for item in response.json()["items"])


def found(client: TestClient, query: str, label: str | None, **body: Any) -> dict[str, Any]:
    response = client.post(SEARCH, json={"query": query, "label": label, **body})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def add_label(engine: Engine, name: str, parent: str | None = None, kind: str = "tag") -> str:
    label_id = ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(
            insert(labels).values(
                id=label_id,
                kind=kind,
                name=name,
                name_key=name.upper(),
                parent_id=parent,
                local_only=False,
            )
        )
    return label_id


def assign(engine: Engine, document_id: str, label_id: str, status: str = "accepted") -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(document_labels).values(
                document_id=document_id,
                label_id=label_id,
                source="user",
                status=status,
                created_at=START,
            )
        )


# --- the tree -----------------------------------------------------------------------------------


def test_an_empty_vault_has_no_labels(client: TestClient) -> None:
    assert client.get(LABELS).json() == {"items": []}


def test_the_tree_lists_every_label_after_its_parent_with_counts(
    client: TestClient, library: Library
) -> None:
    syllabus = library.add("Syllabus.txt", "Week 1", folder_segments=["School", "CSE485"])
    notes = library.add("Notes.txt", "Week 2", folder_segments=["School", "CSE485", "Notes"])
    essay = library.add("Essay.txt", "Draft", folder_segments=["school", "ENG101"])
    library.add("Plan.txt", "Plan", folder_segments=["Work"])
    library.add("Upload.txt", "No folders")
    items = tree(client)
    assert [item["path"] for item in items] == [
        ["School"],
        ["School", "CSE485"],
        ["School", "CSE485", "Notes"],
        ["School", "ENG101"],
        ["Work"],
    ]
    assert [item["documents"] for item in items] == [3, 2, 1, 1, 1]
    ids_by_path = {tuple(item["path"]): item["id"] for item in items}
    school = by_path(client)[("School",)]
    assert school == {
        "id": ids_by_path[("School",)],
        "kind": "folder",
        "name": "School",
        "parent_id": None,
        "path": ["School"],
        "documents": 3,
    }
    assert (
        by_path(client)[("School", "CSE485", "Notes")]["parent_id"]
        == (ids_by_path[("School", "CSE485")])
    )
    assert listed(client, school["id"]) == sorted([syllabus, notes, essay])


def test_counts_leave_out_the_trash_and_the_filter_follows_the_tab(
    client: TestClient, library: Library
) -> None:
    kept = library.add("Kept.txt", "kept", folder_segments=["Archive"])
    gone = library.add("Gone.txt", "gone", folder_segments=["Archive", "Old"])
    library.trash(gone)
    counts = {tuple(item["path"]): item["documents"] for item in tree(client)}
    assert counts == {("Archive",): 1, ("Archive", "Old"): 0}
    archive = by_path(client)[("Archive",)]["id"]
    old = by_path(client)[("Archive", "Old")]["id"]
    assert listed(client, archive) == [kept]
    assert listed(client, old) == []
    assert listed(client, archive, trash=True) == [gone]
    assert listed(client, old, trash=True) == [gone]


# --- the filter matches the label and every label below it ---------------------------------------


def test_a_label_matches_documents_that_carry_only_a_label_below_it(
    client: TestClient, library: Library
) -> None:
    engine = library.engine
    projects = add_label(engine, "Projects")
    capstone = add_label(engine, "Capstone", projects)
    sprint = add_label(engine, "Sprint 1", capstone)
    deep = library.add("Deep.txt", "deep")
    middle = library.add("Middle.txt", "middle")
    top = library.add("Top.txt", "top")
    library.add("Other.txt", "other")
    assign(engine, deep, sprint)
    assign(engine, middle, capstone)
    assign(engine, top, projects)
    assert listed(client, projects) == sorted([deep, middle, top])
    assert listed(client, capstone) == sorted([deep, middle])
    assert listed(client, sprint) == [deep]
    counts = {tuple(item["path"]): item["documents"] for item in tree(client)}
    assert counts == {
        ("Projects",): 3,
        ("Projects", "Capstone"): 2,
        ("Projects", "Capstone", "Sprint 1"): 1,
    }


def test_a_document_under_two_children_is_counted_once(
    client: TestClient, library: Library
) -> None:
    engine = library.engine
    parent = add_label(engine, "Parent")
    first = add_label(engine, "First", parent)
    second = add_label(engine, "Second", parent)
    document = library.add("Both.txt", "both")
    assign(engine, document, first)
    assign(engine, document, second)
    assign(engine, document, parent)
    assert by_path(client)[("Parent",)]["documents"] == 1
    response = client.get(DOCUMENTS, params={"label": parent})
    assert response.json()["total"] == 1


def test_a_rejected_assignment_does_not_count(client: TestClient, library: Library) -> None:
    assert REJECTED in LABEL_STATUSES
    engine = library.engine
    topic = add_label(engine, "Topic", kind="topic")
    rejected = library.add("Rejected.txt", "rejected")
    suggested = library.add("Suggested.txt", "suggested")
    assign(engine, rejected, topic, status=REJECTED)
    with engine.begin() as connection:
        connection.execute(
            insert(document_labels).values(
                document_id=suggested,
                label_id=topic,
                source="ai",
                status="suggested",
                confidence=0.5,
                model="synthetic-model",
                created_at=START,
            )
        )
    assert listed(client, topic) == [suggested]
    assert by_path(client)[("Topic",)]["documents"] == 1


def test_a_parent_loop_written_by_hand_ends(client: TestClient, library: Library) -> None:
    engine = library.engine
    first = add_label(engine, "Loop A")
    second = add_label(engine, "Loop B", first)
    with engine.begin() as connection:
        connection.execute(update(labels).where(labels.c.id == first).values(parent_id=second))
    document = library.add("Loop.txt", "loop")
    assign(engine, document, second)
    assert listed(client, first) == [document]
    assert listed(client, second) == [document]
    assert {item["id"]: item["documents"] for item in tree(client)} == {first: 1, second: 1}


def test_every_count_is_what_the_filter_returns(app: FastAPI, library: Library) -> None:
    """The tree counts and the list filter are two statements; on a random tree they agree."""
    engine = library.engine
    generator = random.Random(485)  # noqa: S311  # a fixed tree, not a secret
    label_ids: list[str] = []
    for number in range(30):
        parent = generator.choice([None, *label_ids]) if label_ids else None
        label_ids.append(add_label(engine, f"Label {number}", parent))
    for number in range(25):
        document = library.add(f"Doc {number}.txt", f"body {number}")
        for label_id in generator.sample(label_ids, generator.randint(0, 3)):
            assign(engine, document, label_id, generator.choice(LABEL_STATUSES))
        if generator.random() < 0.2:
            library.trash(document)
    for node in label_tree(engine):
        page = list_documents(engine, ListQuery(limit=100, label_id=node.id))
        assert page.total == node.documents, node.path


# --- unknown labels and hostile names ------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        ids.new_ulid(),  # the shape of an id, but no label
        "' OR 1=1 --",
        "01ARZ3NDEKTSV4RRFFQ69G5FA'",
        CANARY,
        "",
        "%",
    ],
)
def test_a_label_that_does_not_exist_is_a_fixed_error(
    client: TestClient, library: Library, label: str
) -> None:
    add_indexed(library, "Memo.txt", "A memo about the move.")
    listing = client.get(DOCUMENTS, params={"label": label})
    assert listing.status_code == 404
    assert listing.json() == {"detail": "no such label"}
    for query in ("memo", "  "):
        searched = client.post(SEARCH, json={"query": query, "label": label})
        assert searched.status_code == 404
        assert searched.json() == {"detail": "no such label"}
    with pytest.raises(UnknownLabelError):
        list_documents(library.engine, ListQuery(label_id=label))
    with pytest.raises(UnknownLabelError):
        search(library.engine, SearchQuery(query="memo", label_id=label))


def test_a_label_deleted_while_selected_is_a_fixed_error(
    client: TestClient, library: Library
) -> None:
    engine = library.engine
    label_id = add_label(engine, "Short-lived")
    with engine.begin() as connection:
        connection.execute(labels.delete().where(labels.c.id == label_id))
    assert client.get(DOCUMENTS, params={"label": label_id}).status_code == 404


def test_hostile_names_are_stored_shown_and_filtered_as_text(
    client: TestClient, library: Library
) -> None:
    documents_by_name = {
        name: add_indexed(library, f"File {number}.txt", f"Shared word {number}.")
        for number, name in enumerate(HOSTILE)
    }
    engine = library.engine
    for name, document in documents_by_name.items():
        assign(engine, document, add_label(engine, name))
    shown = {item["name"]: item for item in tree(client)}
    assert set(shown) == set(HOSTILE)
    for name, document in documents_by_name.items():
        assert shown[name]["path"] == [name]
        assert shown[name]["documents"] == 1
        assert listed(client, shown[name]["id"]) == [document]
        assert [
            item["document_id"] for item in found(client, "shared", shown[name]["id"])["items"]
        ] == [document]
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(labels)).scalar_one() == len(
            HOSTILE
        )
        assert connection.execute(select(func.count()).select_from(documents)).scalar_one() == len(
            HOSTILE
        )


def test_hostile_folder_names_from_an_import_become_labels_as_written(
    client: TestClient, library: Library
) -> None:
    document = library.add("Folder.txt", "folder", folder_segments=list(HOSTILE[:3]))
    items = tree(client)
    assert [item["path"] for item in items] == [
        list(HOSTILE[:1]),
        list(HOSTILE[:2]),
        list(HOSTILE[:3]),
    ]
    assert listed(client, items[0]["id"]) == [document]


def test_names_and_ids_are_never_logged(
    client: TestClient, library: Library, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    document = add_indexed(library, "Canary.txt", "Canary text.")
    label_id = add_label(library.engine, CANARY)
    assign(library.engine, document, label_id)
    assert tree(client)[0]["name"] == CANARY
    assert listed(client, label_id) == [document]
    assert found(client, "canary", label_id)["total"] == 1
    client.get(DOCUMENTS, params={"label": CANARY})
    # The test client's own logger (httpx) writes the address it asked for; the app writes none.
    logged = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith("httpx")
    )
    assert CANARY not in logged
    assert label_id not in logged


# --- search ---------------------------------------------------------------------------------------


def test_search_ranks_and_pages_within_the_label(client: TestClient, library: Library) -> None:
    engine = library.engine
    course = add_label(engine, "Course")
    week = add_label(engine, "Week", course)
    inside = []
    for number in range(5):
        document = add_indexed(library, f"memo-{number}.txt", "memo " * (number + 1) + "end")
        if number % 2 == 0:
            assign(engine, document, week)
            inside.append(document)
    everything = found(client, "memo", None, limit=50)
    assert everything["total"] == 5
    filtered = found(client, "memo", course, limit=50)
    assert filtered["total"] == 3
    order = [item["document_id"] for item in filtered["items"]]
    assert sorted(order) == sorted(inside)
    # The same relative order as without the filter: the scope does not change the ranking.
    assert order == [
        item["document_id"] for item in everything["items"] if item["document_id"] in inside
    ]
    pages = [found(client, "memo", course, limit=2, offset=offset) for offset in (0, 2, 4)]
    assert [item["document_id"] for page in pages for item in page["items"]] == order
    assert [page["total"] for page in pages] == [3, 3, 3]
    assert pages[-1]["items"] == []


def test_search_combines_the_label_with_the_type_and_the_trash(
    client: TestClient, library: Library
) -> None:
    engine = library.engine
    label_id = add_label(engine, "Mixed")
    text_file = add_indexed(library, "a.txt", "Invoice for March.")
    markdown = add_indexed(library, "b.md", "Invoice for April.")
    trashed = add_indexed(library, "c.txt", "Invoice for May.")
    for document in (text_file, markdown, trashed):
        assign(engine, document, label_id)
    library.trash(trashed)
    assert [
        item["document_id"] for item in found(client, "invoice", label_id, types=["md"])["items"]
    ] == [markdown]
    assert found(client, "invoice", label_id)["total"] == 2


def test_labels_need_the_token_and_a_vault(anonymous: TestClient) -> None:
    assert anonymous.get(LABELS, headers={"Origin": LOCAL}).status_code == 401
    app = create_app(Settings())
    client = TestClient(
        app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    )
    assert client.get(LABELS).status_code == 503
    assert client.get(DOCUMENTS, params={"label": ids.new_ulid()}).status_code == 503
