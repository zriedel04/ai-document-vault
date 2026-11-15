"""The label repository: folder labels from import paths, the labels a document carries, and the
clean-up of folder labels no document uses (``docs/architecture.md`` section 6).

A folder label is one level of a folder path, under the label of the folder above it, so the same
name in two places (``School/Assignments`` and ``Work/Assignments``) is two labels, and two imports
from the same folder share one. A document from ``School/CSE485/Assignments`` carries all three
levels, each with ``source = import_path``, so a later filter on ``School`` finds everything below
it without walking the tree. Names come cleaned from ``vault.ingest.path_metadata``; this module
only stores and reads them. Names are the owner's own words and are never logged.

Browsing by label (``label_tree``, ``UNDER_LABEL_SQL``) still matches a label *and every label
below it*, because a label the owner or a rule assigns later need not carry its parents, as a
folder's levels do. An assignment the owner rejected (``status = 'rejected'``) does not count;
a suggested one does, as the plan's resolution order shows a pending suggestion. The walk down
the tree uses ``UNION``, so a parent loop written by hand ends instead of running for ever.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Connection, Engine, delete, exists, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vault.db import ids
from vault.db.engine import read_snapshot
from vault.db.schema import MAX_LABEL_CHARS, TRASHED, document_labels, labels

FOLDER: Final = "folder"
IMPORT_PATH: Final = "import_path"
ACCEPTED: Final = "accepted"
# An assignment the owner turned down: it is kept (so it is not suggested again) but never counts.
REJECTED: Final = "rejected"
# A path deeper than this is damage (a parent loop written by hand): reading stops there.
MAX_PATH_LEVELS: Final = 256


def name_key(name: str) -> str:
    """The name as Windows compares folder names, close enough for labels: each character in its
    simple upper case. A character whose upper case is more than one character (``ß``) stays as
    it is, as Windows keeps it, so the key is exactly as long as the name."""
    return "".join(
        upper if len(upper := character.upper()) == 1 else character for character in name
    )


def add_folder_labels(
    connection: Connection, document_id: str, names: Sequence[str], now: datetime
) -> int:
    """Give a document one ``folder`` label for each level of ``names`` (outermost first), making
    the labels that do not exist yet, in the caller's transaction. The caller holds the write lock
    (an import's first statement writes), so the look-up and the insert of a label are not
    overtaken by another import. Returns how many assignments were new."""
    parent: str | None = None
    added = 0
    for name in names:
        if not 1 <= len(name) <= MAX_LABEL_CHARS:
            msg = "a label name has 1 to 255 characters"
            raise ValueError(msg)
        key = name_key(name)
        same_place = (
            labels.c.parent_id.is_(None) if parent is None else labels.c.parent_id == parent
        )
        label_id = connection.execute(
            select(labels.c.id).where(labels.c.kind == FOLDER, same_place, labels.c.name_key == key)
        ).scalar_one_or_none()
        if label_id is None:
            label_id = ids.new_ulid()
            connection.execute(
                labels.insert().values(
                    id=label_id,
                    kind=FOLDER,
                    name=name,
                    name_key=key,
                    parent_id=parent,
                    local_only=False,
                )
            )
        added += connection.execute(
            sqlite_insert(document_labels)
            .values(
                document_id=document_id,
                label_id=label_id,
                source=IMPORT_PATH,
                status=ACCEPTED,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["document_id", "label_id", "source"])
        ).rowcount
        parent = label_id
    return added


def delete_unused_folder_labels(connection: Connection) -> int:
    """Remove the folder labels that no document carries and no label sits under, deepest first,
    in the caller's transaction (a permanent delete's). Returns how many went."""
    removed = 0
    while True:
        child = labels.alias("child")
        gone = connection.execute(
            delete(labels).where(
                labels.c.kind == FOLDER,
                ~exists().where(document_labels.c.label_id == labels.c.id),
                ~exists().where(child.c.parent_id == labels.c.id),
            )
        ).rowcount
        if gone == 0:
            return removed
        removed += gone


@dataclass(frozen=True)
class LabelDetail:
    id: str
    kind: str
    name: str
    path: tuple[str, ...]  # the names from the outermost label down to this one
    source: str
    status: str


def labels_of(connection: Connection, document_id: str) -> tuple[LabelDetail, ...]:
    """The labels a document carries, each with its path, sorted by kind and path."""
    assigned = connection.execute(
        select(
            document_labels.c.label_id, document_labels.c.source, document_labels.c.status
        ).where(document_labels.c.document_id == document_id)
    ).all()
    if not assigned:
        return ()
    tree = labels.alias("tree")
    known = (
        select(labels.c.id, labels.c.kind, labels.c.name, labels.c.parent_id)
        .where(labels.c.id.in_([row.label_id for row in assigned]))
        .cte("known", recursive=True)
    )
    known = known.union(
        select(tree.c.id, tree.c.kind, tree.c.name, tree.c.parent_id).join(
            known, tree.c.id == known.c.parent_id
        )
    )
    rows = {row.id: row for row in connection.execute(select(known)).all()}
    tree_of = {label_id: (row.name, row.parent_id) for label_id, row in rows.items()}
    details = (
        LabelDetail(
            id=row.label_id,
            kind=rows[row.label_id].kind,
            name=rows[row.label_id].name,
            path=label_path(tree_of, row.label_id),
            source=row.source,
            status=row.status,
        )
        for row in assigned
    )
    return tuple(
        sorted(
            details,
            key=lambda label: label_order(label.kind, label.path, label.source, label.id),
        )
    )


def label_path(tree: Mapping[str, tuple[str, str | None]], label_id: str) -> tuple[str, ...]:
    """The names from the outermost label down to ``label_id``, from each label's ``(name,
    parent_id)`` in ``tree``. A parent that ``tree`` does not hold ends the path, and so does a
    path deeper than ``MAX_PATH_LEVELS`` (a parent loop written by hand)."""
    names: list[str] = []
    current: str | None = label_id
    while current is not None and current in tree and len(names) < MAX_PATH_LEVELS:
        name, current = tree[current]
        names.append(name)
    return tuple(reversed(names))


def label_order(
    kind: object, path: Sequence[object], source: object, label_id: object
) -> tuple[str, list[str], str, str]:
    """The order labels are listed in, everywhere (the detail panel and the export): by kind, then
    path as Windows compares folder names, then source, then id, so the order never depends on how
    the database returned the rows. A value a hand edit left empty sorts as ``""``."""
    return (
        "" if kind is None else str(kind),
        [name_key(str(name)) for name in path],
        "" if source is None else str(source),
        "" if label_id is None else str(label_id),
    )


class UnknownLabelError(LookupError):
    """A label filter names no label. The message is fixed text and never repeats the id."""

    def __init__(self) -> None:
        super().__init__("no such label")


# The ids of the documents a label filter keeps: those that carry the label ``:label_id`` or a
# label below it, by an assignment that was not rejected (see the module docstring). Fixed text
# with one bound parameter, written once and used by the document list (``vault.db.documents``)
# and by keyword search (``vault.retrieval.keyword``), so the two filters cannot disagree. The
# caller adds the state of the document (the trash or not).
UNDER_LABEL_SQL: Final = """SELECT document_labels.document_id FROM document_labels
    WHERE document_labels.status != 'rejected' AND document_labels.label_id IN (
        WITH RECURSIVE under_label(id) AS (
            SELECT :label_id
            UNION
            SELECT labels.id FROM labels JOIN under_label ON labels.parent_id = under_label.id
        )
        SELECT id FROM under_label
    )"""


def check_label(connection: Connection, label_id: str) -> None:
    """Raise ``UnknownLabelError`` unless ``label_id`` names a label, in the caller's snapshot, so a
    filter on a label deleted a moment ago is a fixed error and not an empty page. A value that
    is not an id at all is refused before it reaches the database."""
    if not ids.is_ulid(label_id) or (
        connection.execute(select(labels.c.id).where(labels.c.id == label_id)).first() is None
    ):
        raise UnknownLabelError


@dataclass(frozen=True)
class LabelNode:
    id: str
    kind: str
    name: str
    parent_id: str | None
    path: tuple[str, ...]  # the names from the outermost label down to this one
    documents: int  # documents not in the trash that carry it or a label below it


# For every label, the documents not in the trash that carry it or a label below it (the same
# rule as ``UNDER_LABEL_SQL``, for every label at once: a test compares the two on every label).
_COUNTS = text(
    """WITH RECURSIVE below(root, id) AS (
        SELECT id, id FROM labels
        UNION
        SELECT below.root, labels.id FROM labels JOIN below ON labels.parent_id = below.id
    )
    SELECT below.root AS label_id, count(DISTINCT document_labels.document_id) AS documents
    FROM below
    JOIN document_labels ON document_labels.label_id = below.id
        AND document_labels.status != 'rejected'
    JOIN documents ON documents.id = document_labels.document_id AND documents.state != :trashed
    GROUP BY below.root"""
)


def label_tree(engine: Engine) -> tuple[LabelNode, ...]:
    """Every label with its parent, its path and how many documents in use it holds (itself and
    every label below it), in the library's order (``label_order``): a parent comes before its
    children, and the children of a label follow it, so the list reads as the tree. One snapshot
    for both reads."""
    with read_snapshot(engine) as connection:
        rows = connection.execute(
            select(labels.c.id, labels.c.kind, labels.c.name, labels.c.parent_id)
        ).all()
        counts = {
            row.label_id: int(row.documents)
            for row in connection.execute(_COUNTS, {"trashed": TRASHED})
        }
    tree_of = {row.id: (row.name, row.parent_id) for row in rows}
    nodes = (
        LabelNode(
            id=row.id,
            kind=row.kind,
            name=row.name,
            parent_id=row.parent_id,
            path=label_path(tree_of, row.id),
            documents=counts.get(row.id, 0),
        )
        for row in rows
    )
    return tuple(sorted(nodes, key=lambda node: label_order(node.kind, node.path, "", node.id)))
