"""The labels as a tree, with how many documents each holds, for browsing the library by label
(``docs/architecture.md`` section 6). The names are the owner's own words: they go to the page
that asked for them and are never logged."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import Engine

from vault.api.documents import LabelKind
from vault.core.security import LocalUser, current_user
from vault.db.labels import label_tree

router = APIRouter(tags=["labels"])


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


@router.get("/labels")
async def read_labels(
    request: Request,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> LabelTree:
    """Every label as a tree in one list: each label names its parent and its path, and counts
    the documents in use that carry it or any label below it. The list is in tree order, so a
    page can show it as it comes, indented by the length of each path."""
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    nodes = await run_in_threadpool(label_tree, engine)
    return LabelTree(items=[LabelNode.model_validate(node, from_attributes=True) for node in nodes])
