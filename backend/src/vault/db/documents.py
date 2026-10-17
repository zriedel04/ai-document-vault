"""The document repository (``db/`` holds the repositories, ``docs/architecture.md`` section 19):
one page of the document list, sorted on the server (section 16: no screen loads more than a
page of documents).

A document's name is the file name of its first import source (the earliest, ties broken by id),
because a document has no name of its own until the owner renames it in a later phase. Its size
and type are those of its current version's blob. The list holds the documents in use, or with
``in_trash`` the documents in the trash, never both.

Pages are by offset. A document added while the owner pages through the list moves the rest by
one, which a library of this size can live with; the order itself is total (every sort ends with
the id), so the same request on unchanged data always gives the same page.

The names are the owner's own file names: they go to the page that asked for them and are never
logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import ColumnElement, Engine, Row, func, select

from vault.db.engine import read_snapshot
from vault.db.schema import TRASHED, blobs, document_versions, documents, import_sources

MAX_PAGE: Final = 100
DEFAULT_PAGE: Final = 50


class SortKey(StrEnum):
    ADDED = "added"
    NAME = "name"
    SIZE = "size"


class Order(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class ListQuery:
    sort: SortKey = SortKey.ADDED
    order: Order = Order.DESC
    limit: int = DEFAULT_PAGE
    offset: int = 0
    in_trash: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE:
            msg = f"limit must be between 1 and {MAX_PAGE}"
            raise ValueError(msg)
        if self.offset < 0:
            msg = "offset must not be negative"
            raise ValueError(msg)


@dataclass(frozen=True)
class DocumentSummary:
    id: str
    name: (
        str | None
    )  # None only when a document has no import source, which the importer never writes
    extension: str
    mime: str
    size: int
    state: str
    added_at: datetime
    trashed_at: datetime | None
    sources: int


@dataclass(frozen=True)
class DocumentPage:
    items: tuple[DocumentSummary, ...]
    total: int


def _utc(value: datetime) -> datetime:
    """The database stores UTC without a zone; say so, so the page does not read it as local."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def list_documents(engine: Engine, query: ListQuery) -> DocumentPage:
    ranked = (
        select(
            import_sources.c.document_id,
            import_sources.c.original_filename.label("name"),
            func.row_number()
            .over(
                partition_by=import_sources.c.document_id,
                order_by=(import_sources.c.imported_at, import_sources.c.id),
            )
            .label("rank"),
            func.count().over(partition_by=import_sources.c.document_id).label("sources"),
        )
    ).subquery("ranked")
    first = (
        select(ranked.c.document_id, ranked.c.name, ranked.c.sources)
        .where(ranked.c.rank == 1)
        .subquery("first_source")
    )

    where = documents.c.state == TRASHED if query.in_trash else documents.c.state != TRASHED
    base = (
        select(
            documents.c.id,
            first.c.name,
            blobs.c.ext,
            blobs.c.mime,
            blobs.c.size,
            documents.c.state,
            documents.c.created_at,
            documents.c.trashed_at,
            func.coalesce(first.c.sources, 0).label("sources"),
            func.count().over().label("total"),  # the total, without a second statement
        )
        .select_from(documents)
        .join(
            document_versions,
            (document_versions.c.id == documents.c.current_version_id)
            & (document_versions.c.document_id == documents.c.id),
        )
        .join(blobs, blobs.c.sha256 == document_versions.c.blob_sha256)
        .outerjoin(first, first.c.document_id == documents.c.id)
        .where(where)
    )

    key: ColumnElement[object] = {
        SortKey.ADDED: documents.c.created_at,
        SortKey.NAME: first.c.name.collate("NOCASE"),
        SortKey.SIZE: blobs.c.size,
    }[query.sort]
    if query.order is Order.ASC:
        ordering = (key.asc(), documents.c.id.asc())
    else:
        ordering = (key.desc(), documents.c.id.desc())

    # One snapshot for both reads: the driver opens no transaction for a read, so without it a
    # commit between the page and the fallback count could pair an old page with a new total.
    with read_snapshot(engine) as connection:
        rows = connection.execute(
            base.order_by(*ordering).limit(query.limit).offset(query.offset)
        ).all()
        if rows:
            total = int(rows[0].total)
        else:  # past the end (or nothing at all): the page is empty, the count says how far it goes
            total = connection.execute(
                select(func.count()).select_from(base.subquery())
            ).scalar_one()
    return DocumentPage(items=tuple(_summary(row) for row in rows), total=total)


def _summary(row: Row[tuple[object, ...]]) -> DocumentSummary:
    values = row._mapping
    trashed_at = values["trashed_at"]
    return DocumentSummary(
        id=values["id"],
        name=values["name"],
        extension=values["ext"],
        mime=values["mime"],
        size=values["size"],
        state=values["state"],
        added_at=_utc(values["created_at"]),
        trashed_at=None if trashed_at is None else _utc(trashed_at),
        sources=values["sources"],
    )
