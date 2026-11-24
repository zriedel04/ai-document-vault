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

The owner's own labels (collections and tags, ``OWNER_KINDS``) are made, renamed, moved and
deleted here too, and assigned with ``source = user``; see the section at the end. A folder label
is never changed by those functions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Final

from sqlalchemy import Connection, Engine, Row, String, column, delete, exists, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vault.db import ids
from vault.db.engine import read_snapshot, write_transaction
from vault.db.schema import MAX_LABEL_CHARS, TRASHED, document_labels, documents, labels

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


# --- the owner's own labels -----------------------------------------------------------------------
#
# The plan's "manual tags and collections" (section 12): labels the owner makes, renames, nests and
# deletes, and assigns to documents with ``source = user``. They are kept apart from the ``folder``
# labels, which only an import makes and only a permanent delete removes: none of the functions
# below changes a label of another kind, and none puts an owner's label under one. A label nests
# only under a label of its own kind, so a collection's tree holds collections and a tag's tags.
# Every function takes the name already cleaned (``vault.ingest.path_metadata.owner_label_name``,
# the rule a folder name follows) and runs in one write transaction, so a check (a name is free, a
# move makes no loop) still holds when the change is written.

COLLECTION: Final = "collection"
TAG: Final = "tag"
OWNER_KINDS: Final = (COLLECTION, TAG)
USER: Final = "user"
# How deep the owner's labels nest: as deep as folder labels go (``path_metadata.MAX_DEPTH``; a
# test checks that they agree), well inside what ``label_path`` reads.
MAX_OWNER_DEPTH: Final = 64


class LabelRefusal(Enum):
    """Why a change to the owner's labels was not made. Each has a fixed message in the API."""

    NO_LABEL = "no_label"  # the label is not there (or the id is not an id)
    NO_PARENT = "no_parent"  # the parent named is not there
    NO_DOCUMENT = "no_document"
    NOT_OWNERS = "not_owners"  # a folder label (or another kind): not the owner's to change
    PARENT_KIND = "parent_kind"  # the parent is of another kind
    NAME_TAKEN = "name_taken"  # the parent already holds a label of this kind and name
    LOOP = "loop"  # the label would sit under itself or a label below it
    TOO_DEEP = "too_deep"  # the move would nest a label deeper than MAX_OWNER_DEPTH
    HAS_CHILDREN = "has_children"  # a label with labels under it is not deleted


def _check_name(name: str) -> str:
    if not 1 <= len(name) <= MAX_LABEL_CHARS:
        msg = "a label name has 1 to 255 characters"
        raise ValueError(msg)
    return name_key(name)


def _label_row(connection: Connection, label_id: str | None) -> Row[Any, Any, Any] | None:
    if not ids.is_ulid(label_id):
        return None
    return connection.execute(
        select(labels.c.id, labels.c.kind, labels.c.parent_id).where(labels.c.id == label_id)
    ).first()


def _name_taken(
    connection: Connection, kind: str, parent: str | None, key: str, other_than: str | None
) -> bool:
    same_place = labels.c.parent_id.is_(None) if parent is None else labels.c.parent_id == parent
    query = select(labels.c.id).where(labels.c.kind == kind, same_place, labels.c.name_key == key)
    if other_than is not None:
        query = query.where(labels.c.id != other_than)
    return connection.execute(query).first() is not None


def _ancestors(connection: Connection, label_id: str) -> list[str] | None:
    """The ids from ``label_id`` up to the top, itself first, or None when the walk meets a loop
    (a hand edit) or passes ``MAX_PATH_LEVELS``."""
    found: list[str] = []
    current: str | None = label_id
    while current is not None:
        if current in found or len(found) >= MAX_PATH_LEVELS:
            return None
        found.append(current)
        current = connection.execute(
            select(labels.c.parent_id).where(labels.c.id == current)
        ).scalar_one_or_none()
    return found


_HEIGHT = text(
    """WITH RECURSIVE below(id, depth) AS (
        SELECT :label_id, 1
        UNION
        SELECT labels.id, below.depth + 1 FROM labels JOIN below ON labels.parent_id = below.id
        WHERE below.depth < :limit
    )
    SELECT max(depth) FROM below"""
)


def _place(
    connection: Connection, kind: str, parent_id: str | None, moving: str | None
) -> LabelRefusal | None:
    """Why a label of ``kind`` (``moving``, or a new one) cannot go under ``parent_id``, or None."""
    if parent_id is None:
        return None
    parent = _label_row(connection, parent_id)
    if parent is None:
        return LabelRefusal.NO_PARENT
    if parent.kind != kind:
        return LabelRefusal.PARENT_KIND
    above = _ancestors(connection, parent_id)
    if above is None or (moving is not None and moving in above):
        return LabelRefusal.LOOP
    height = 1
    if moving is not None:
        height = int(
            connection.execute(
                _HEIGHT, {"label_id": moving, "limit": MAX_OWNER_DEPTH + 1}
            ).scalar_one()
        )
    if len(above) + height > MAX_OWNER_DEPTH:
        return LabelRefusal.TOO_DEEP
    return None


def _node(connection: Connection, label_id: str) -> LabelNode:
    rows = connection.execute(
        select(labels.c.id, labels.c.kind, labels.c.name, labels.c.parent_id)
    ).all()
    tree_of = {row.id: (row.name, row.parent_id) for row in rows}
    row = next(row for row in rows if row.id == label_id)
    # The documents not in the trash under it, by the filter's own fragment: the tree's count.
    under = (
        text(UNDER_LABEL_SQL).bindparams(label_id=label_id).columns(column("document_id", String))
    )
    count = connection.execute(
        select(func.count())
        .select_from(documents)
        .where(documents.c.state != TRASHED, documents.c.id.in_(under))
    ).scalar_one()
    return LabelNode(
        id=row.id,
        kind=row.kind,
        name=row.name,
        parent_id=row.parent_id,
        path=label_path(tree_of, row.id),
        documents=int(count),
    )


def create_owner_label(
    engine: Engine, kind: str, name: str, parent_id: str | None
) -> LabelNode | LabelRefusal:
    """Make one of the owner's labels, at the top or under ``parent_id`` (a label of the same
    kind), and return it as the tree shows it."""
    if kind not in OWNER_KINDS:
        msg = "not a kind of label the owner makes"
        raise ValueError(msg)
    key = _check_name(name)
    with write_transaction(engine) as connection:
        refusal = _place(connection, kind, parent_id, None)
        if refusal is not None:
            return refusal
        if _name_taken(connection, kind, parent_id, key, None):
            return LabelRefusal.NAME_TAKEN
        label_id = ids.new_ulid()
        connection.execute(
            labels.insert().values(
                id=label_id,
                kind=kind,
                name=name,
                name_key=key,
                parent_id=parent_id,
                local_only=False,
            )
        )
        return _node(connection, label_id)


# What ``change_owner_label`` leaves as it is when it is not given.
KEEP: Final = object()


def change_owner_label(
    engine: Engine, label_id: str, name: str | None, parent_id: object = KEEP
) -> LabelNode | LabelRefusal:
    """Rename one of the owner's labels (``name``, None to keep it) and move it (``parent_id``: a
    label of the same kind, None for the top, ``KEEP`` to leave it), and return it as the tree
    shows it. A move under the label itself or a label below it is refused (``LOOP``)."""
    key = None if name is None else _check_name(name)
    with write_transaction(engine) as connection:
        row = _label_row(connection, label_id)
        if row is None:
            return LabelRefusal.NO_LABEL
        if row.kind not in OWNER_KINDS:
            return LabelRefusal.NOT_OWNERS
        parent: str | None
        if parent_id is KEEP:
            parent = row.parent_id
        elif parent_id is None or isinstance(parent_id, str):
            parent = parent_id
        else:
            msg = "a parent is a label id or None"
            raise TypeError(msg)
        if parent != row.parent_id:
            refusal = _place(connection, row.kind, parent, label_id)
            if refusal is not None:
                return refusal
        if key is None:
            key = connection.execute(
                select(labels.c.name_key).where(labels.c.id == label_id)
            ).scalar_one()
        if _name_taken(connection, row.kind, parent, key, label_id):
            return LabelRefusal.NAME_TAKEN
        values: dict[str, object] = {"parent_id": parent}
        if name is not None:
            values |= {"name": name, "name_key": key}
        connection.execute(labels.update().where(labels.c.id == label_id).values(**values))
        return _node(connection, label_id)


def delete_owner_label(engine: Engine, label_id: str) -> int | LabelRefusal:
    """Delete one of the owner's labels and every assignment of it; the documents stay. A label
    with labels under it is refused (``HAS_CHILDREN``): delete or move them first. Returns how
    many assignments went."""
    with write_transaction(engine) as connection:
        row = _label_row(connection, label_id)
        if row is None:
            return LabelRefusal.NO_LABEL
        if row.kind not in OWNER_KINDS:
            return LabelRefusal.NOT_OWNERS
        if connection.execute(select(labels.c.id).where(labels.c.parent_id == label_id)).first():
            return LabelRefusal.HAS_CHILDREN
        removed = connection.execute(
            delete(document_labels).where(document_labels.c.label_id == label_id)
        ).rowcount
        connection.execute(delete(labels).where(labels.c.id == label_id))
        return removed


def _assignable(connection: Connection, document_id: str, label_id: str) -> LabelRefusal | None:
    if not ids.is_ulid(document_id) or (
        connection.execute(select(documents.c.id).where(documents.c.id == document_id)).first()
        is None
    ):
        return LabelRefusal.NO_DOCUMENT
    row = _label_row(connection, label_id)
    if row is None:
        return LabelRefusal.NO_LABEL
    if row.kind not in OWNER_KINDS:
        return LabelRefusal.NOT_OWNERS
    return None


def assign_owner_label(
    engine: Engine, document_id: str, label_id: str, now: datetime
) -> bool | LabelRefusal:
    """Give a document (in use or in the trash) one of the owner's labels, ``source = user`` and
    accepted. Returns whether the assignment is new; assigning it again changes nothing, and an
    assignment of the owner's that a later feature marked otherwise becomes accepted again."""
    with write_transaction(engine) as connection:
        refusal = _assignable(connection, document_id, label_id)
        if refusal is not None:
            return refusal
        existing = connection.execute(
            select(document_labels.c.status).where(
                document_labels.c.document_id == document_id,
                document_labels.c.label_id == label_id,
                document_labels.c.source == USER,
            )
        ).scalar_one_or_none()
        if existing == ACCEPTED:
            return False
        if existing is not None:
            connection.execute(
                document_labels.update()
                .where(
                    document_labels.c.document_id == document_id,
                    document_labels.c.label_id == label_id,
                    document_labels.c.source == USER,
                )
                .values(status=ACCEPTED)
            )
            return True
        connection.execute(
            document_labels.insert().values(
                document_id=document_id,
                label_id=label_id,
                source=USER,
                status=ACCEPTED,
                created_at=now,
            )
        )
        return True


def unassign_owner_label(engine: Engine, document_id: str, label_id: str) -> bool | LabelRefusal:
    """Take one of the owner's labels off a document: its ``source = user`` assignment goes, and
    an assignment from any other source stays. Returns whether there was one to remove."""
    with write_transaction(engine) as connection:
        refusal = _assignable(connection, document_id, label_id)
        if refusal is not None:
            return refusal
        return bool(
            connection.execute(
                delete(document_labels).where(
                    document_labels.c.document_id == document_id,
                    document_labels.c.label_id == label_id,
                    document_labels.c.source == USER,
                )
            ).rowcount
        )
