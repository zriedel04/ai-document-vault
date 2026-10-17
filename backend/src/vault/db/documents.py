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

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import ColumnElement, Engine, Row, func, select

from vault.db import ids
from vault.db.engine import read_snapshot
from vault.db.schema import (
    JOB_STAGES,
    TRASHED,
    blobs,
    document_versions,
    documents,
    import_sources,
    jobs,
)

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
            # The total in the same statement as the page, so both come from one snapshot: the
            # driver does not open a transaction for a read, so two statements could see a write
            # between them.
            func.count().over().label("total"),
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

    with engine.connect() as connection:
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


@dataclass(frozen=True)
class SourceDetail:
    id: str
    filename: str
    original_path: str | None
    folders: tuple[str, ...]
    original_mtime: datetime | None
    method: str
    batch_id: str
    imported_at: datetime


@dataclass(frozen=True)
class JobDetail:
    stage: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None  # the exception's class name, never its message (vault.jobs.queue)
    finished_at: datetime | None


@dataclass(frozen=True)
class DocumentDetail:
    summary: DocumentSummary
    sha256: str
    local_only: bool
    version_id: str
    version_no: int
    version_origin: str
    sources: tuple[SourceDetail, ...]  # oldest first; the first one names the document
    jobs: tuple[JobDetail, ...]  # in the order of the pipeline's stages


def get_document(engine: Engine, document_id: str) -> DocumentDetail | None:
    """Everything the detail panel shows about one document, or None if there is no such document
    (in use or in the trash). The three reads share one snapshot, so the parts agree."""
    if not ids.is_ulid(document_id):
        return None
    with read_snapshot(engine) as connection:
        row = connection.execute(
            select(
                documents.c.id,
                documents.c.state,
                documents.c.local_only,
                documents.c.created_at,
                documents.c.trashed_at,
                document_versions.c.id.label("version_id"),
                document_versions.c.version_no,
                document_versions.c.origin,
                blobs.c.sha256,
                blobs.c.ext,
                blobs.c.mime,
                blobs.c.size,
            )
            .select_from(documents)
            .join(
                document_versions,
                (document_versions.c.id == documents.c.current_version_id)
                & (document_versions.c.document_id == documents.c.id),
            )
            .join(blobs, blobs.c.sha256 == document_versions.c.blob_sha256)
            .where(documents.c.id == document_id)
        ).one_or_none()
        if row is None:
            return None
        sources = connection.execute(
            select(import_sources)
            .where(import_sources.c.document_id == document_id)
            .order_by(import_sources.c.imported_at, import_sources.c.id)
        ).all()
        job_rows = connection.execute(select(jobs).where(jobs.c.document_id == document_id)).all()

    source_details = tuple(_source(source) for source in sources)
    summary = DocumentSummary(
        id=row.id,
        name=source_details[0].filename if source_details else None,
        extension=row.ext,
        mime=row.mime,
        size=row.size,
        state=row.state,
        added_at=_utc(row.created_at),
        trashed_at=None if row.trashed_at is None else _utc(row.trashed_at),
        sources=len(source_details),
    )
    stage_order = {stage: position for position, stage in enumerate(JOB_STAGES)}
    job_details = tuple(
        JobDetail(
            stage=job.stage,
            status=job.status,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            last_error=job.last_error,
            finished_at=None if job.finished_at is None else _utc(job.finished_at),
        )
        for job in sorted(job_rows, key=lambda job: (stage_order[job.stage], job.id))
    )
    return DocumentDetail(
        summary=summary,
        sha256=row.sha256,
        local_only=bool(row.local_only),
        version_id=row.version_id,
        version_no=row.version_no,
        version_origin=row.origin,
        sources=source_details,
        jobs=job_details,
    )


def _source(row: Row[Any]) -> SourceDetail:
    folders = json.loads(row.folder_segments)
    if not isinstance(folders, list) or not all(isinstance(name, str) for name in folders):
        # The table's CHECK makes it a JSON list; only a list of names is a folder path.
        msg = "an import source's folders are not a list of names"
        raise ValueError(msg)
    return SourceDetail(
        id=row.id,
        filename=row.original_filename,
        original_path=row.original_path,
        folders=tuple(folders),
        original_mtime=None if row.original_mtime is None else _utc(row.original_mtime),
        method=row.method,
        batch_id=row.import_batch_id,
        imported_at=_utc(row.imported_at),
    )
