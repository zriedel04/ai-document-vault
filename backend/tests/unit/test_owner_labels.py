"""The owner's own labels: making, renaming, nesting and deleting collections and tags, assigning
them to documents, and filtering by them (synthetic names and text only)."""

from __future__ import annotations

import logging
import typing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.unit.test_documents_api import Library
from tests.unit.test_search import add as add_indexed
from vault.api.labels import MAX_NAME_INPUT, NAME_REFUSED, REFUSALS, OwnerKind
from vault.core.config import Settings
from vault.db import ids
from vault.db.labels import (
    MAX_OWNER_DEPTH,
    OWNER_KINDS,
    LabelRefusal,
    change_owner_label,
    create_owner_label,
)
from vault.db.schema import LABEL_KINDS, MAX_LABEL_CHARS, document_labels, documents, labels
from vault.ingest import path_metadata
from vault.main import create_app

from ..conftest import LOCAL

LABELS = "/api/labels"
DOCUMENTS = "/api/documents"
SEARCH = "/api/search"
CANARY = "Patient-Diagnosis-Canary"


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


def make(
    client: TestClient, name: str, kind: str = "collection", parent: str | None = None
) -> dict[str, Any]:
    response = client.post(LABELS, json={"kind": kind, "name": name, "parent_id": parent})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def refused(response: Any, refusal: LabelRefusal) -> None:
    status, detail = REFUSALS[refusal]
    assert (response.status_code, response.json()) == (status, {"detail": detail})


def tree(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get(LABELS)
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()["items"]}


def listed(client: TestClient, label: str) -> list[str]:
    response = client.get(DOCUMENTS, params={"label": label})
    assert response.status_code == 200, response.text
    return sorted(item["id"] for item in response.json()["items"])


def assign(client: TestClient, document: str, label: str) -> Any:
    return client.put(f"{DOCUMENTS}/{document}/labels/{label}")


def unassign(client: TestClient, document: str, label: str) -> Any:
    return client.delete(f"{DOCUMENTS}/{document}/labels/{label}")


def owner_labels(detail: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (label["id"], label["source"], label["status"])
        for label in detail["labels"]
        if label["kind"] in OWNER_KINDS
    ]


# --- the rules agree with the rest of the code ----------------------------------------------------


def test_the_owner_kinds_are_label_kinds_and_the_api_lists_them() -> None:
    assert set(OWNER_KINDS) < set(LABEL_KINDS)
    assert "folder" not in OWNER_KINDS
    assert typing.get_args(OwnerKind) == OWNER_KINDS


def test_owner_labels_nest_as_deep_as_folders() -> None:
    assert MAX_OWNER_DEPTH == path_metadata.MAX_DEPTH


def test_every_refusal_has_fixed_text() -> None:
    assert set(REFUSALS) == set(LabelRefusal)
    for status, detail in REFUSALS.values():
        assert status in {404, 409}
        assert detail
        assert "{" not in detail


# --- making a label ------------------------------------------------------------------------------


def test_a_new_collection_is_in_the_tree_and_the_filter(client: TestClient) -> None:
    made = make(client, "School")
    assert made == {
        "id": made["id"],
        "kind": "collection",
        "name": "School",
        "parent_id": None,
        "path": ["School"],
        "documents": 0,
    }
    assert ids.is_ulid(made["id"])
    assert tree(client)[made["id"]] == made
    assert listed(client, made["id"]) == []


def test_names_are_cleaned_as_folder_names_are(client: TestClient) -> None:
    raw = "  Te‮rm​\tone\n\u0007notes.. . "
    made = make(client, raw)
    assert made["name"] == path_metadata.owner_label_name(raw) == "Term one notes"
    # The same rule as a folder of the same name.
    assert path_metadata.folder_names([raw]) == (made["name"],)
    composed = make(client, "Café")
    assert composed["name"] == "Café"


@pytest.mark.parametrize(
    "name",
    ["", " ", "...", ". .", "​‮", "\u0000\u0001", "x" * (MAX_LABEL_CHARS + 1)],
)
def test_a_name_with_nothing_left_or_too_long_is_refused(client: TestClient, name: str) -> None:
    response = client.post(LABELS, json={"kind": "collection", "name": name})
    assert response.status_code == 422
    assert client.get(LABELS).json() == {"items": []}


def test_the_longest_name_is_kept_whole(client: TestClient) -> None:
    name = "y" * MAX_LABEL_CHARS
    assert make(client, f"  {name}  ")["name"] == name
    response = client.post(LABELS, json={"kind": "tag", "name": "z" * (MAX_NAME_INPUT + 1)})
    assert response.status_code == 422


def test_a_refused_name_is_fixed_text(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    response = client.post(
        LABELS, json={"kind": "collection", "name": CANARY + "x" * MAX_LABEL_CHARS}
    )
    assert response.json() == {"detail": NAME_REFUSED}
    response = client.post(LABELS, json={"kind": CANARY, "name": CANARY})
    assert response.status_code == 422
    assert CANARY not in response.text
    assert CANARY not in caplog.text


@pytest.mark.parametrize("kind", ["folder", "topic", "Collection", ""])
def test_only_collections_and_tags_are_made(client: TestClient, kind: str) -> None:
    response = client.post(LABELS, json={"kind": kind, "name": "Name"})
    assert response.status_code == 422
    with pytest.raises(ValueError, match="kind"):
        create_owner_label(client.app.state.engine, kind, "Name", None)  # type: ignore[attr-defined]


def test_a_name_is_unique_per_kind_and_parent_as_windows_compares(client: TestClient) -> None:
    school = make(client, "School")
    refused(
        client.post(LABELS, json={"kind": "collection", "name": "SCHOOL"}),
        LabelRefusal.NAME_TAKEN,
    )
    refused(
        client.post(LABELS, json={"kind": "collection", "name": "school. "}),
        LabelRefusal.NAME_TAKEN,
    )
    # Another kind, another parent: another label.
    assert make(client, "school", kind="tag")["name"] == "school"
    assert make(client, "School", parent=school["id"])["path"] == ["School", "School"]
    # A character whose upper case is two characters is compared as it is (name_key).
    make(client, "Straße")
    assert make(client, "STRASSE")["name"] == "STRASSE"


def test_a_folder_of_the_same_name_is_another_label(client: TestClient, library: Library) -> None:
    library.add("Notes.txt", "notes", folder_segments=["School"])
    made = make(client, "School")
    kinds = sorted(item["kind"] for item in tree(client).values())
    assert kinds == ["collection", "folder"]
    assert made["documents"] == 0


def test_a_label_goes_only_under_its_own_kind(client: TestClient, library: Library) -> None:
    library.add("Notes.txt", "notes", folder_segments=["Work"])
    folder = next(iter(tree(client)))
    tag = make(client, "Urgent", kind="tag")
    refused(
        client.post(LABELS, json={"kind": "collection", "name": "A", "parent_id": tag["id"]}),
        LabelRefusal.PARENT_KIND,
    )
    refused(
        client.post(LABELS, json={"kind": "collection", "name": "A", "parent_id": folder}),
        LabelRefusal.PARENT_KIND,
    )
    for parent in (ids.new_ulid(), CANARY, ""):
        response = client.post(LABELS, json={"kind": "tag", "name": "A", "parent_id": parent})
        refused(response, LabelRefusal.NO_PARENT)
        assert CANARY not in response.text
    assert make(client, "Today", kind="tag", parent=tag["id"])["path"] == ["Urgent", "Today"]


def test_labels_nest_at_most_the_deepest_level(client: TestClient) -> None:
    parent = None
    for level in range(MAX_OWNER_DEPTH):
        parent = make(client, f"Level {level}", parent=parent)["id"]
    refused(
        client.post(LABELS, json={"kind": "collection", "name": "Too deep", "parent_id": parent}),
        LabelRefusal.TOO_DEEP,
    )


# --- renaming and moving -------------------------------------------------------------------------


def test_a_rename_cleans_and_keeps_the_place(client: TestClient) -> None:
    top = make(client, "Top")
    child = make(client, "Old", parent=top["id"])
    response = client.patch(f"{LABELS}/{child['id']}", json={"name": " New​. "})
    assert response.status_code == 200, response.text
    assert response.json()["path"] == ["Top", "New"]
    assert response.json()["parent_id"] == top["id"]
    # Its own name in another case is no clash with itself.
    assert client.patch(f"{LABELS}/{child['id']}", json={"name": "NEW"}).json()["name"] == "NEW"
    assert client.patch(f"{LABELS}/{child['id']}", json={"name": "  "}).status_code == 422
    assert client.patch(f"{LABELS}/{child['id']}", json={"name": ""}).status_code == 422
    assert client.patch(f"{LABELS}/{child['id']}", json={}).json()["name"] == "NEW"


def test_a_rename_to_a_name_taken_is_refused(client: TestClient) -> None:
    make(client, "Taken")
    other = make(client, "Other")
    refused(
        client.patch(f"{LABELS}/{other['id']}", json={"name": "taken"}), LabelRefusal.NAME_TAKEN
    )
    assert tree(client)[other["id"]]["name"] == "Other"


def test_a_move_takes_the_labels_below_and_their_documents(
    client: TestClient, library: Library
) -> None:
    school = make(client, "School")
    work = make(client, "Work")
    course = make(client, "Course", parent=school["id"])
    week = make(client, "Week", parent=course["id"])
    document = library.add("Week.txt", "week")
    assert assign(client, document, week["id"]).status_code == 200
    assert listed(client, school["id"]) == [document]
    response = client.patch(f"{LABELS}/{course['id']}", json={"parent_id": work["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["path"] == ["Work", "Course"]
    assert response.json()["documents"] == 1
    assert tree(client)[week["id"]]["path"] == ["Work", "Course", "Week"]
    assert listed(client, school["id"]) == []
    assert listed(client, work["id"]) == [document]
    top = client.patch(f"{LABELS}/{course['id']}", json={"parent_id": None})
    assert top.json()["path"] == ["Course"]
    assert top.json()["parent_id"] is None


def test_a_move_under_itself_or_below_it_is_refused(client: TestClient) -> None:
    top = make(client, "Top")
    middle = make(client, "Middle", parent=top["id"])
    bottom = make(client, "Bottom", parent=middle["id"])
    for target in (top, middle, bottom):
        refused(
            client.patch(f"{LABELS}/{top['id']}", json={"parent_id": target["id"]}),
            LabelRefusal.LOOP,
        )
    assert tree(client)[top["id"]]["parent_id"] is None


def test_a_move_into_a_name_taken_or_too_deep_is_refused(client: TestClient) -> None:
    first = make(client, "First")
    make(client, "Same", parent=first["id"])
    same = make(client, "same")
    refused(
        client.patch(f"{LABELS}/{same['id']}", json={"parent_id": first["id"]}),
        LabelRefusal.NAME_TAKEN,
    )
    # A chain of half the depth moved under another chain of half plus one is one level too deep.
    half = MAX_OWNER_DEPTH // 2
    chain = []
    parent = None
    for level in range(half + 1):
        parent = make(client, f"Deep {level}", parent=parent)["id"]
        chain.append(parent)
    moving = make(client, "Moving")["id"]
    below = moving
    for level in range(half - 1):
        below = make(client, f"Below {level}", parent=below)["id"]
    refused(
        client.patch(f"{LABELS}/{moving}", json={"parent_id": chain[-1]}), LabelRefusal.TOO_DEEP
    )
    assert client.patch(f"{LABELS}/{moving}", json={"parent_id": chain[-2]}).status_code == 200


def test_a_parent_loop_written_by_hand_is_not_moved_into(client: TestClient) -> None:
    engine = client.app.state.engine  # type: ignore[attr-defined]
    first = make(client, "Loop A")
    second = make(client, "Loop B", parent=first["id"])
    with engine.begin() as connection:
        connection.execute(
            labels.update().where(labels.c.id == first["id"]).values(parent_id=second["id"])
        )
    moving = make(client, "Moving")
    refused(
        client.patch(f"{LABELS}/{moving['id']}", json={"parent_id": second["id"]}),
        LabelRefusal.LOOP,
    )


def test_a_folder_label_is_not_changed_here(client: TestClient, library: Library) -> None:
    document = library.add("Notes.txt", "notes", folder_segments=["School"])
    folder = next(iter(tree(client)))
    collection = make(client, "Mine")
    refused(client.patch(f"{LABELS}/{folder}", json={"name": "Renamed"}), LabelRefusal.NOT_OWNERS)
    refused(
        client.patch(f"{LABELS}/{folder}", json={"parent_id": collection["id"]}),
        LabelRefusal.NOT_OWNERS,
    )
    refused(client.delete(f"{LABELS}/{folder}"), LabelRefusal.NOT_OWNERS)
    refused(assign(client, document, folder), LabelRefusal.NOT_OWNERS)
    refused(unassign(client, document, folder), LabelRefusal.NOT_OWNERS)
    assert tree(client)[folder]["name"] == "School"
    assert listed(client, folder) == [document]


@pytest.mark.parametrize("label", [ids.new_ulid(), CANARY, "%", "' OR 1=1 --"])
def test_an_unknown_label_is_a_fixed_404(client: TestClient, library: Library, label: str) -> None:
    document = library.add("Notes.txt", "notes")
    for response in (
        client.patch(f"{LABELS}/{label}", json={"name": "New"}),
        client.delete(f"{LABELS}/{label}"),
        assign(client, document, label),
        unassign(client, document, label),
    ):
        refused(response, LabelRefusal.NO_LABEL)
        assert CANARY not in response.text


def test_the_repository_refuses_a_parent_of_another_type(client: TestClient) -> None:
    engine = client.app.state.engine  # type: ignore[attr-defined]
    made = make(client, "Typed")
    with pytest.raises(TypeError):
        change_owner_label(engine, made["id"], None, 5)


# --- deleting ------------------------------------------------------------------------------------


def test_a_delete_removes_the_assignments_and_keeps_the_documents(
    client: TestClient, library: Library
) -> None:
    engine = library.engine
    label = make(client, "Gone")
    kept = make(client, "Kept")
    first = library.add("One.txt", "one", folder_segments=["Folder"])
    second = library.add("Two.txt", "two")
    for document in (first, second):
        assert assign(client, document, label["id"]).status_code == 200
        assert assign(client, document, kept["id"]).status_code == 200
    response = client.delete(f"{LABELS}/{label['id']}")
    assert response.status_code == 204
    assert label["id"] not in tree(client)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(documents)).scalar_one() == 2
        assert (
            connection.execute(
                select(func.count())
                .select_from(document_labels)
                .where(document_labels.c.label_id == label["id"])
            ).scalar_one()
            == 0
        )
    assert listed(client, kept["id"]) == sorted([first, second])
    detail = client.get(f"{DOCUMENTS}/{first}").json()
    assert [label["kind"] for label in detail["labels"]] == ["collection", "folder"]
    refused(client.delete(f"{LABELS}/{label['id']}"), LabelRefusal.NO_LABEL)


def test_a_label_with_labels_under_it_is_not_deleted(client: TestClient) -> None:
    top = make(client, "Top")
    child = make(client, "Child", parent=top["id"])
    refused(client.delete(f"{LABELS}/{top['id']}"), LabelRefusal.HAS_CHILDREN)
    assert client.delete(f"{LABELS}/{child['id']}").status_code == 204
    assert client.delete(f"{LABELS}/{top['id']}").status_code == 204
    assert client.get(LABELS).json() == {"items": []}


# --- assigning ------------------------------------------------------------------------------------


def test_an_assignment_is_the_owners_and_filters_the_list_and_search(
    client: TestClient, library: Library
) -> None:
    school = make(client, "School")
    course = make(client, "CSE 485", parent=school["id"])
    inside = add_indexed(library, "Inside.txt", "A syllabus for the course.")
    outside = add_indexed(library, "Outside.txt", "Another syllabus.")
    response = assign(client, inside, course["id"])
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["id"] == inside
    assert owner_labels(detail) == [(course["id"], "user", "accepted")]
    shown = next(label for label in detail["labels"] if label["id"] == course["id"])
    assert shown["path"] == ["School", "CSE 485"]
    # Again: nothing changes.
    assert owner_labels(assign(client, inside, course["id"]).json()) == owner_labels(detail)
    assert listed(client, school["id"]) == [inside]
    assert listed(client, course["id"]) == [inside]
    assert tree(client)[school["id"]]["documents"] == 1
    for label in (school, course):
        searched = client.post(SEARCH, json={"query": "syllabus", "label": label["id"]})
        assert [item["document_id"] for item in searched.json()["items"]] == [inside]
    assert outside not in listed(client, school["id"])


def test_an_unassignment_removes_only_the_owners_assignment(
    client: TestClient, library: Library
) -> None:
    engine = library.engine
    tag = make(client, "Later", kind="tag")
    document = library.add("Doc.txt", "doc")
    other = library.add("Other.txt", "other")
    assign(client, document, tag["id"])
    assign(client, other, tag["id"])
    # An assignment from another source (an AI suggestion, a later phase) stays.
    with engine.begin() as connection:
        connection.execute(
            document_labels.insert().values(
                document_id=document,
                label_id=tag["id"],
                source="ai",
                status="suggested",
                confidence=0.5,
                model="synthetic-model",
                created_at=library.now,
            )
        )
    response = unassign(client, document, tag["id"])
    assert response.status_code == 200
    assert owner_labels(response.json()) == [(tag["id"], "ai", "suggested")]
    assert owner_labels(unassign(client, document, tag["id"]).json()) == [
        (tag["id"], "ai", "suggested")
    ]
    assert listed(client, tag["id"]) == sorted([document, other])
    assert owner_labels(unassign(client, other, tag["id"]).json()) == []
    assert listed(client, tag["id"]) == [document]


def test_an_assignment_marked_otherwise_is_accepted_again(
    client: TestClient, library: Library
) -> None:
    engine = library.engine
    tag = make(client, "Again", kind="tag")
    document = library.add("Doc.txt", "doc")
    assign(client, document, tag["id"])
    with engine.begin() as connection:
        connection.execute(document_labels.update().values(status="rejected"))
    assert listed(client, tag["id"]) == []
    assert owner_labels(assign(client, document, tag["id"]).json()) == [
        (tag["id"], "user", "accepted")
    ]


def test_a_document_in_the_trash_can_be_labelled(client: TestClient, library: Library) -> None:
    label = make(client, "Archive")
    document = library.add("Old.txt", "old")
    library.trash(document)
    assert assign(client, document, label["id"]).status_code == 200
    assert listed(client, label["id"]) == []
    response = client.get(DOCUMENTS, params={"label": label["id"], "trash": True})
    assert [item["id"] for item in response.json()["items"]] == [document]


@pytest.mark.parametrize("document", [ids.new_ulid(), CANARY])
def test_an_unknown_document_is_a_fixed_404(client: TestClient, document: str) -> None:
    label = make(client, "Somewhere")
    for response in (
        assign(client, document, label["id"]),
        unassign(client, document, label["id"]),
    ):
        refused(response, LabelRefusal.NO_DOCUMENT)
        assert CANARY not in response.text


def test_a_permanent_delete_keeps_the_owners_labels(client: TestClient, library: Library) -> None:
    label = make(client, "Stays")
    document = library.add("Gone.txt", "gone", folder_segments=["Folder"])
    assign(client, document, label["id"])
    library.trash(document)
    assert client.delete(f"{DOCUMENTS}/{document}").status_code == 204
    # The folder label nobody carries goes; the owner's collection stays, now empty.
    assert [(item["kind"], item["documents"]) for item in tree(client).values()] == [
        ("collection", 0)
    ]


# --- privacy and access --------------------------------------------------------------------------


def test_names_are_never_logged(
    client: TestClient, library: Library, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    document = library.add("Doc.txt", "doc")
    made = make(client, CANARY)
    client.post(LABELS, json={"kind": "collection", "name": CANARY})  # taken
    client.patch(f"{LABELS}/{made['id']}", json={"name": CANARY + " 2"})
    assign(client, document, made["id"])
    unassign(client, document, made["id"])
    client.delete(f"{LABELS}/{made['id']}")
    logged = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith("httpx")
    )
    assert CANARY not in logged
    assert made["id"] not in logged
    assert document not in logged


def test_changes_need_the_token_the_origin_and_a_vault(
    anonymous: TestClient, client: TestClient, library: Library
) -> None:
    label = make(client, "Guarded")
    document = library.add("Doc.txt", "doc")
    changes = [
        ("POST", LABELS, {"kind": "tag", "name": "x"}),
        ("PATCH", f"{LABELS}/{label['id']}", {"name": "x"}),
        ("DELETE", f"{LABELS}/{label['id']}", None),
        ("PUT", f"{DOCUMENTS}/{document}/labels/{label['id']}", None),
        ("DELETE", f"{DOCUMENTS}/{document}/labels/{label['id']}", None),
    ]
    for method, url, body in changes:
        response = anonymous.request(method, url, json=body, headers={"Origin": LOCAL})
        assert response.status_code == 401, (method, url)
        no_origin = client.request(method, url, json=body, headers={"Origin": "http://evil.test"})
        assert no_origin.status_code == 403, (method, url)
    assert tree(client)[label["id"]]["name"] == "Guarded"
    app = create_app(Settings())
    empty = TestClient(
        app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    )
    for method, url, body in changes:
        assert empty.request(method, url, json=body).status_code == 503, (method, url)
