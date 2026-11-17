"""The labels as a tree, with how many documents each holds, for browsing the library by label
(``docs/architecture.md`` section 6), and the owner's own labels: collections and tags they make,
rename, nest and delete, and assign to documents (section 12, "manual tags and collections").

Folder labels stay the imports' own: these routes refuse to change one or to assign one (409).
The names are the owner's own words: they go to the page that asked for them and are never logged,
and every refusal is fixed text that repeats nothing from the request."""

from __future__ import annotations

import logging
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from vault.api.documents import DocumentDetail, LabelKind, _detail
from vault.core.logging import log_event, metric
from vault.core.security import LocalUser, current_user
from vault.db.labels import (
    KEEP,
    MAX_OWNER_DEPTH,
    LabelRefusal,
    assign_owner_label,
    change_owner_label,
    create_owner_label,
    delete_owner_label,
    label_tree,
    unassign_owner_label,
)
from vault.db.schema import MAX_LABEL_CHARS
from vault.ingest.path_metadata import owner_label_name
from vault.jobs.queue import utc_now

logger = logging.getLogger(__name__)
router = APIRouter(tags=["labels"])

# The kinds of label the owner makes (vault.db.labels.OWNER_KINDS, spelled out for the schema).
OwnerKind = Literal["collection", "tag"]
# The longest name the request may carry before it is cleaned: room for white space and hidden
# characters that cleaning removes, and a bound on what is read.
MAX_NAME_INPUT: Final = 4 * MAX_LABEL_CHARS

# Each refusal's status and fixed text. None of them repeats a name or an id.
REFUSALS: Final[dict[LabelRefusal, tuple[int, str]]] = {
    LabelRefusal.NO_LABEL: (404, "no such label"),
    LabelRefusal.NO_PARENT: (404, "no such parent label"),
    LabelRefusal.NO_DOCUMENT: (404, "no such document"),
    LabelRefusal.NOT_OWNERS: (409, "folder labels are kept by imports and cannot be changed here"),
    LabelRefusal.PARENT_KIND: (409, "a label goes only under a label of its own kind"),
    LabelRefusal.NAME_TAKEN: (409, "a label of this kind and name is already there"),
    LabelRefusal.LOOP: (409, "a label cannot go under itself or a label below it"),
    LabelRefusal.TOO_DEEP: (409, f"labels nest at most {MAX_OWNER_DEPTH} levels deep"),
    LabelRefusal.HAS_CHILDREN: (409, "the label has labels under it"),
}
NAME_REFUSED: Final = "a label name needs 1 to 255 characters other than spaces and hidden ones"


class LabelNode(BaseModel):
    id: str
    kind: LabelKind
    name: str
    # The label above this one, or None at the top of the tree.
    parent_id: str | None
    # The names from the outermost label down to this one.
    path: list[str]
    # The documents not in the trash that carry this label or a label below it: the documents
    # the list and the search keep when they are filtered by it.
    documents: int


class LabelTree(BaseModel):
    # Every label, each parent before its children, ordered by kind and then path as Windows
    # compares folder names (vault.db.labels.label_order).
    items: list[LabelNode]


class NewLabel(BaseModel):
    kind: OwnerKind
    name: str = Field(min_length=1, max_length=MAX_NAME_INPUT)
    # A label of the same kind to put it under, or None (left out) for the top of the tree.
    parent_id: str | None = None


class LabelChange(BaseModel):
    # The new name; left out (or null) keeps the name.
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_INPUT)
    # The new parent, a label of the same kind, or null for the top; left out keeps the parent.
    parent_id: str | None = None


def _engine(request: Request) -> Engine:
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    return engine


def _refuse(refusal: LabelRefusal) -> HTTPException:
    status, detail = REFUSALS[refusal]
    return HTTPException(status_code=status, detail=detail)


def _name(text: str) -> str:
    name = owner_label_name(text)
    if name is None:
        raise HTTPException(status_code=422, detail=NAME_REFUSED)
    return name


def _responses(*refusals: LabelRefusal) -> dict[int | str, dict[str, str]]:
    found: dict[int | str, list[str]] = {}
    for refusal in refusals:
        status, detail = REFUSALS[refusal]
        found.setdefault(status, []).append(detail)
    return {status: {"description": "; ".join(texts)} for status, texts in found.items()}


@router.get("/labels")
async def read_labels(
    request: Request,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> LabelTree:
    """Every label as a tree in one list: each label names its parent and its path, and counts
    the documents in use that carry it or any label below it. The list is in tree order, so a
    page can show it as it comes, indented by the length of each path."""
    nodes = await run_in_threadpool(label_tree, _engine(request))
    return LabelTree(items=[LabelNode.model_validate(node, from_attributes=True) for node in nodes])


@router.post(
    "/labels",
    status_code=201,
    responses=_responses(
        LabelRefusal.NO_PARENT,
        LabelRefusal.PARENT_KIND,
        LabelRefusal.NAME_TAKEN,
        LabelRefusal.TOO_DEEP,
    ),
)
async def create_label(
    request: Request,
    body: NewLabel,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> LabelNode:
    """Make a collection or a tag, at the top or under ``parent_id`` (a label of the same kind),
    and return it as the tree shows it. The name is cleaned as a folder name is (Unicode NFC;
    control, bidirectional and invisible characters removed; white space collapsed; trailing dots
    and spaces dropped); a name that is then empty or longer than 255 characters is a 422. Names
    are compared as Windows compares them (``School`` and ``school`` are one name), and one kind
    holds one label of a name under each parent (409)."""
    name = _name(body.name)
    result = await run_in_threadpool(
        create_owner_label, _engine(request), body.kind, name, body.parent_id
    )
    if isinstance(result, LabelRefusal):
        raise _refuse(result)
    log_event(logger, "labels.created")
    return LabelNode.model_validate(result, from_attributes=True)


@router.patch(
    "/labels/{label_id}",
    responses=_responses(
        LabelRefusal.NO_LABEL,
        LabelRefusal.NO_PARENT,
        LabelRefusal.NOT_OWNERS,
        LabelRefusal.PARENT_KIND,
        LabelRefusal.NAME_TAKEN,
        LabelRefusal.LOOP,
        LabelRefusal.TOO_DEEP,
    ),
)
async def change_label(
    request: Request,
    label_id: str,
    body: LabelChange,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> LabelNode:
    """Rename a collection or a tag (``name``, cleaned as on creation), move it (``parent_id``: a
    label of the same kind, or null for the top), or both, and return it as the tree shows it.
    Its documents and the labels below it move with it. A move under the label itself or a label
    below it is refused (409), and so is a folder label (409): an import's folders are changed
    only by the imports."""
    name = None if body.name is None else _name(body.name)
    parent = body.parent_id if "parent_id" in body.model_fields_set else KEEP
    result = await run_in_threadpool(change_owner_label, _engine(request), label_id, name, parent)
    if isinstance(result, LabelRefusal):
        raise _refuse(result)
    log_event(logger, "labels.changed")
    return LabelNode.model_validate(result, from_attributes=True)


@router.delete(
    "/labels/{label_id}",
    status_code=204,
    responses=_responses(LabelRefusal.NO_LABEL, LabelRefusal.NOT_OWNERS, LabelRefusal.HAS_CHILDREN),
)
async def delete_label(
    request: Request,
    label_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> Response:
    """Delete a collection or a tag and every assignment of it. The documents stay where they
    are. A label with labels under it is refused (409): delete or move those first. A folder
    label is refused (409)."""
    result = await run_in_threadpool(delete_owner_label, _engine(request), label_id)
    if isinstance(result, LabelRefusal):
        raise _refuse(result)
    # A count only: the id came in the request.
    log_event(logger, "labels.deleted", assignments=metric(result))
    return Response(status_code=204)


_ASSIGN_REFUSALS: Final = (
    LabelRefusal.NO_DOCUMENT,
    LabelRefusal.NO_LABEL,
    LabelRefusal.NOT_OWNERS,
)


@router.put("/documents/{document_id}/labels/{label_id}", responses=_responses(*_ASSIGN_REFUSALS))
async def assign_label(
    request: Request,
    document_id: str,
    label_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> DocumentDetail:
    """Put a document (in use or in the trash) in a collection or give it a tag, as the owner's
    own assignment (``source`` user, accepted), and return the document as it now is. Doing it
    again changes nothing. A folder label is refused (409)."""
    engine = _engine(request)
    result = await run_in_threadpool(assign_owner_label, engine, document_id, label_id, utc_now())
    if isinstance(result, LabelRefusal):
        raise _refuse(result)
    log_event(logger, "labels.assigned", added=metric(int(result)))
    return await _detail(engine, document_id)


@router.delete(
    "/documents/{document_id}/labels/{label_id}", responses=_responses(*_ASSIGN_REFUSALS)
)
async def unassign_label(
    request: Request,
    document_id: str,
    label_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> DocumentDetail:
    """Take a collection or a tag off a document (the owner's own assignment; any other source's
    stays) and return the document as it now is. A document without it is returned unchanged. A
    folder label is refused (409)."""
    engine = _engine(request)
    result = await run_in_threadpool(unassign_owner_label, engine, document_id, label_id)
    if isinstance(result, LabelRefusal):
        raise _refuse(result)
    log_event(logger, "labels.unassigned", removed=metric(int(result)))
    return await _detail(engine, document_id)
