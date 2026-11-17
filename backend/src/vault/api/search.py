"""Keyword search: one page of the documents whose text matches a query, with snippets."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, WithJsonSchema
from sqlalchemy import Engine

from vault.core.security import LocalUser, current_user
from vault.db.labels import UnknownLabelError
from vault.retrieval import keyword
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

# An instant with an explicit offset (RFC 3339, ``T`` and ``Z`` in capitals, at most the
# microseconds the database stores): ``2026-03-01T05:00:00Z``, ``2026-03-01T00:00:00.5-05:00``.
_INSTANT: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|([+-])(\d{2}):(\d{2}))",
    re.ASCII,
)
_INSTANT_TEXT: Final = "a time with a UTC offset between the years 1 and 9999"

# The canonical extensions of vault.ingest.validate.MIME_TYPES, spelled out so the API schema
# lists them; a test checks that the two agree.
Extension = Literal["pdf", "docx", "png", "jpg", "txt", "md"]


# The schema says what the page must send; the model accepts any JSON so that ``parse_instant``
# refuses it with fixed text.
Instant = Annotated[
    Any,
    WithJsonSchema({"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]}),
]


class SearchRequest(BaseModel):
    query: str
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE
    offset: Annotated[int, Field(ge=0, le=MAX_OFFSET)] = 0
    # Only documents of these types; empty or left out means every type.
    types: Annotated[list[Extension], Field(max_length=10)] = []
    # Only documents that carry this label (an id from GET /api/labels) or a label below it;
    # left out or null means every document.
    label: str | None = None
    # Only documents imported at or after this instant and at or before ``imported_to`` (both
    # inclusive, either may be left out). Instants, not calendar days: the page turns the days the
    # owner picks into the first and last instant of those days in the browser's own time zone.
    # Any JSON value here, read by ``parse_instant``, so every refusal (a number or an object
    # too) is fixed text: the framework's own 422 would repeat the value it refused.
    imported_from: Instant = None
    imported_to: Instant = None


def parse_instant(value: object, field: str) -> datetime:
    """``value`` (RFC 3339 with an offset) as naive UTC, as the database stores times.

    Raises ``ValueError`` with fixed text naming only ``field`` for anything else: a time with no
    offset (which would mean the server's zone, not the owner's), a day or a time that does not
    exist, more than microseconds, a value that is not a string, or an instant outside the years
    1 to 9999 once it is in UTC. That is the range of Python's ``datetime`` and of the four-digit
    year the database writes; the conversion is plain arithmetic on a fixed offset, never the C
    library, whose range on Windows is narrower."""
    match = _INSTANT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{field} is not {_INSTANT_TEXT}")
    year, month, day, hour, minute, second, fraction, sign, offset_hours, offset_minutes = (
        match.groups()
    )
    try:
        offset = timedelta(0)
        if sign is not None:
            if int(offset_minutes) > 59:
                raise ValueError
            offset = timedelta(hours=int(offset_hours), minutes=int(offset_minutes))
            if sign == "-":
                offset = -offset
        local = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            int((fraction or "").ljust(6, "0")),
            tzinfo=timezone(offset),
        )
        return local.astimezone(UTC).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ValueError(f"{field} is not {_INSTANT_TEXT}") from None


def imported_range(body: SearchRequest) -> tuple[datetime | None, datetime | None]:
    """The request's import bounds as naive UTC; ``ValueError`` with fixed text otherwise."""
    start = (
        None if body.imported_from is None else parse_instant(body.imported_from, "imported_from")
    )
    end = None if body.imported_to is None else parse_instant(body.imported_to, "imported_to")
    if start is not None and end is not None and start > end:
        msg = "imported_from is after imported_to"
        raise ValueError(msg)
    return start, end


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
    # The name and each section title, with the words the query matched marked.
    name_highlight: list[Segment]
    section_highlight: list[list[Segment]]


class SearchResults(BaseModel):
    items: list[SearchHit]
    total: int
    limit: int
    offset: int


@router.post(
    "/search",
    responses={
        404: {"description": "No such label"},
        422: {"description": "The query or the import dates cannot be searched"},
    },
)
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
    message does not repeat it. ``label`` keeps only the documents that carry that label or one
    below it, in the same statement as the rest of the scope, so ``total`` and the pages count
    the filtered documents; an id that names no label is a 404 whose message does not repeat
    it. ``imported_from`` and ``imported_to`` keep the documents first imported at or between
    those instants (``parse_instant``), in the same statement; a bound that is not an instant
    with an offset, or a start after the end, is a 422 whose message names the field and not
    the value. Nothing here is logged."""
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    try:
        imported_from, imported_to = imported_range(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    query = SearchQuery(
        query=body.query,
        limit=body.limit,
        offset=body.offset,
        extensions=tuple(sorted(set(body.types))),
        label_id=body.label,
        imported_from=imported_from,
        imported_to=imported_to,
    )
    try:
        page = await run_in_threadpool(search, engine, query)
    except QueryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except UnknownLabelError:
        raise HTTPException(status_code=404, detail="no such label") from None
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
                snippet=[_segment(part) for part in hit.snippet],
                matches=hit.matches,
                name_highlight=[_segment(part) for part in hit.name_highlight],
                section_highlight=[
                    [_segment(part) for part in title] for title in hit.section_highlight
                ],
            )
            for hit in page.items
        ],
        total=page.total,
        limit=body.limit,
        offset=body.offset,
    )


def _segment(part: keyword.Segment) -> Segment:
    return Segment(text=part.text, match=part.match)
