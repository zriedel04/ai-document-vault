"""The document list: one page at a time, sorted on the server."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import Engine

from vault.core.security import LocalUser, current_user
from vault.db.documents import (
    DEFAULT_PAGE,
    MAX_PAGE,
    ListQuery,
    Order,
    SortKey,
    list_documents,
)

router = APIRouter(tags=["documents"])

# Beyond any library; it keeps the number inside SQLite's integer range.
MAX_OFFSET = 1_000_000_000


# The states of vault.db.schema.DOCUMENT_STATES, spelled out so the API schema lists them.
State = Literal["processing", "searchable", "ready", "failed", "trashed"]


class DocumentItem(BaseModel):
    id: str
    name: str | None
    extension: str
    mime: str
    size: int
    state: State
    added_at: datetime
    trashed_at: datetime | None
    sources: int


class DocumentList(BaseModel):
    items: list[DocumentItem]
    total: int
    limit: int
    offset: int


@router.get("/documents")
async def read_documents(
    request: Request,
    _user: Annotated[LocalUser, Depends(current_user)],
    sort: SortKey = SortKey.ADDED,
    order: Order = Order.DESC,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    trash: bool = False,
) -> DocumentList:
    """One page of documents in use (or, with ``trash``, of the trash), sorted by when they were
    added, their name or their size, newest first unless ``order`` says otherwise. ``total``
    counts every document the list holds, so the page can show how many pages there are."""
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    query = ListQuery(sort=sort, order=order, limit=limit, offset=offset, in_trash=trash)
    page = await run_in_threadpool(list_documents, engine, query)
    return DocumentList(
        items=[DocumentItem.model_validate(item, from_attributes=True) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )
