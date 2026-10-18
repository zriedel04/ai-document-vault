"""Keyword search: one page of the documents whose text matches a query, with snippets."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from vault.core.security import LocalUser, current_user
from vault.retrieval.keyword import (
    DEFAULT_PAGE,
    MAX_PAGE,
    QueryError,
    SearchQuery,
    search,
)

router = APIRouter(tags=["search"])

# Beyond any library; it keeps the number inside SQLite's integer range.
MAX_OFFSET = 1_000_000_000

# The canonical extensions of vault.ingest.validate.MIME_TYPES, spelled out so the API schema
# lists them; a test checks that the two agree.
Extension = Literal["pdf", "docx", "png", "jpg", "txt", "md"]


class SearchRequest(BaseModel):
    query: str
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE
    offset: Annotated[int, Field(ge=0, le=MAX_OFFSET)] = 0
    # Only documents of these types; empty or left out means every type.
    types: Annotated[list[Extension], Field(max_length=10)] = []


class Segment(BaseModel):
    text: str
    match: bool


class SearchHit(BaseModel):
    document_id: str
    name: str | None
    extension: str
    chunk_id: int
    page: int
    char_start: int
    char_end: int
    section_path: list[str]
    snippet: list[Segment]
    matches: int


class SearchResults(BaseModel):
    items: list[SearchHit]
    total: int
    limit: int
    offset: int


@router.post("/search", responses={422: {"description": "The query cannot be searched"}})
async def search_documents(
    request: Request,
    body: SearchRequest,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> SearchResults:
    """One page of the documents in use whose current text matches ``query``, best first, each
    with its best passage: the page it is on, its place in that page's text, its section and a
    snippet whose matched words are marked. ``total`` counts the matching documents.

    The query goes in the body, never in the address, because it says what the owner is looking
    for. Quotes make a phrase, a trailing ``*`` a prefix, and every term must match; a query with
    no letter or digit matches nothing. A query over 500 characters or 16 terms is a 422 whose
    message does not repeat it. Nothing here is logged."""
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    query = SearchQuery(
        query=body.query,
        limit=body.limit,
        offset=body.offset,
        extensions=tuple(sorted(set(body.types))),
    )
    try:
        page = await run_in_threadpool(search, engine, query)
    except QueryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return SearchResults(
        items=[
            SearchHit(
                document_id=hit.document_id,
                name=hit.name,
                extension=hit.extension,
                chunk_id=hit.chunk_id,
                page=hit.page,
                char_start=hit.char_start,
                char_end=hit.char_end,
                section_path=list(hit.section_path),
                snippet=[Segment(text=part.text, match=part.match) for part in hit.snippet],
                matches=hit.matches,
            )
            for hit in page.items
        ],
        total=page.total,
        limit=body.limit,
        offset=body.offset,
    )
