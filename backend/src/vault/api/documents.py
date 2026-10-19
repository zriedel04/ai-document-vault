"""The document list (one page at a time, sorted on the server), one document's details, its
original file, its extracted text, retrying its failed processing, moving it to the trash and
back, and deleting it for good from the trash."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import Engine

from vault.core.logging import log_event, metric
from vault.core.security import LocalUser, current_user
from vault.db.documents import (
    DEFAULT_PAGE,
    MAX_PAGE,
    DeleteRefusal,
    ListQuery,
    Order,
    SortKey,
    get_document,
    get_original,
    list_documents,
    move_to_trash,
    restore,
)
from vault.db.pages import (
    DEFAULT_WINDOW,
    MAX_WINDOW,
    MAX_WINDOW_CHARS,
    PageDataError,
    read_current_pages,
)
from vault.jobs.queue import JobQueue, RetryRefusal, utc_now
from vault.jobs.worker import WorkerPool
from vault.storage.blobs import BlobIntegrityError, BlobStore
from vault.storage.purge import purge_document

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])

# Beyond any library; it keeps the number inside SQLite's integer range.
MAX_OFFSET = 1_000_000_000


# The states of vault.db.schema.DOCUMENT_STATES, spelled out so the API schema lists them.
State = Literal["processing", "searchable", "ready", "failed", "trashed"]
# And of VERSION_ORIGINS, IMPORT_METHODS, JOB_STAGES and JOB_STATUSES.
Origin = Literal["import", "user", "ai_copy"]
Method = Literal["upload", "folder", "watch"]
Stage = Literal["extract", "ocr", "metadata", "chunk", "embed", "enrich"]
JobStatus = Literal["queued", "running", "done", "failed"]


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


class Source(BaseModel):
    id: str
    filename: str
    original_path: str | None
    folders: list[str]
    original_mtime: datetime | None
    method: Method
    batch_id: str
    imported_at: datetime


class Job(BaseModel):
    stage: Stage
    status: JobStatus
    attempts: int
    max_attempts: int
    last_error: str | None
    finished_at: datetime | None


class DocumentDetail(DocumentItem):
    sha256: str
    local_only: bool
    version_id: str
    version_no: int
    version_origin: Origin
    import_sources: list[Source]
    jobs: list[Job]


@router.get("/documents/{document_id}", responses={404: {"description": "No such document"}})
async def read_document(
    request: Request,
    document_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> DocumentDetail:
    """One document, in use or in the trash: what the list shows, plus its hash, current version,
    every import (oldest first; the first names the document) and its processing jobs (in the
    order of the pipeline). An unknown id, or one that is not an id at all, is a 404 whose message
    does not repeat it."""
    return await _detail(_engine(request), document_id)


def _engine(request: Request) -> Engine:
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    return engine


async def _detail(engine: Engine, document_id: str) -> DocumentDetail:
    detail = await run_in_threadpool(get_document, engine, document_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="no such document")
    return DocumentDetail(
        **DocumentItem.model_validate(detail.summary, from_attributes=True).model_dump(),
        sha256=detail.sha256,
        local_only=detail.local_only,
        version_id=detail.version_id,
        version_no=detail.version_no,
        version_origin=detail.version_origin,
        import_sources=[Source.model_validate(s, from_attributes=True) for s in detail.sources],
        jobs=[Job.model_validate(job, from_attributes=True) for job in detail.jobs],
    )


@router.post("/documents/{document_id}/trash", responses={404: {"description": "No such document"}})
async def trash_document(
    request: Request,
    document_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> DocumentDetail:
    """Move a document to the trash and return it as it now is. Nothing is deleted: its file,
    versions, imports and jobs stay, and a restore brings it back in the state it had. A document
    already in the trash stays there with the time it first went there."""
    engine = _engine(request)
    if not await run_in_threadpool(move_to_trash, engine, document_id, utc_now()):
        raise HTTPException(status_code=404, detail="no such document")
    return await _detail(engine, document_id)


@router.post(
    "/documents/{document_id}/restore", responses={404: {"description": "No such document"}}
)
async def restore_document(
    request: Request,
    document_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> DocumentDetail:
    """Bring a document out of the trash, in the state it had, and return it as it now is. A
    document that is not in the trash is returned unchanged."""
    engine = _engine(request)
    if not await run_in_threadpool(restore, engine, document_id):
        raise HTTPException(status_code=404, detail="no such document")
    return await _detail(engine, document_id)


@router.post(
    "/documents/{document_id}/retry",
    responses={
        404: {"description": "No such document"},
        409: {"description": "The document is in the trash"},
    },
)
async def retry_document(
    request: Request,
    document_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> DocumentDetail:
    """Queue the document's failed processing again, with all its attempts, and return it as it
    now is: each stage whose latest job failed runs again, and a document in the ``failed`` state
    goes back to ``processing``. A document with nothing failed is returned unchanged, so pressing
    Retry twice queues the work once. A document in the trash is refused (409): restore it first.
    """
    engine = _engine(request)
    result = await run_in_threadpool(JobQueue(engine).retry_document, document_id)
    if result is RetryRefusal.IN_TRASH:
        raise HTTPException(status_code=409, detail="the document is in the trash")
    if isinstance(result, RetryRefusal):
        raise HTTPException(status_code=404, detail="no such document")
    if result:
        workers: WorkerPool | None = request.app.state.workers
        if workers is not None:
            workers.wake()
    # A count only: the id came in the request.
    log_event(logger, "documents.retried", jobs=metric(result))
    return await _detail(engine, document_id)


@router.delete(
    "/documents/{document_id}",
    status_code=204,
    responses={
        404: {"description": "No such document"},
        409: {"description": "The document is not in the trash"},
    },
)
async def delete_document(
    request: Request,
    document_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> Response:
    """Delete a document in the trash for good: its versions, imports, pages and jobs, and each
    stored file that no other document uses. It cannot be undone. A document in use is refused
    (409) and left as it is; it must be moved to the trash first. An unknown id is a 404, also when
    the document was deleted by an earlier request.

    The answer is 204 once the rows are gone, even if a file could not be removed now: the start-up
    sweep moves such a file to ``blobs/orphans/`` (``vault.storage.purge``)."""
    engine: Engine | None = request.app.state.engine
    store: BlobStore | None = request.app.state.blobs
    if engine is None or store is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    result = await run_in_threadpool(purge_document, engine, store, document_id)
    if isinstance(result, DeleteRefusal):
        if result is DeleteRefusal.NOT_IN_TRASH:
            raise HTTPException(status_code=409, detail="the document is not in the trash")
        raise HTTPException(status_code=404, detail="no such document")
    # Counts only: the id came in the request, and the name and hash are the owner's.
    log_event(
        logger,
        "documents.deleted",
        blobs_removed=metric(result.blobs_removed),
        blobs_kept=metric(result.blobs_kept),
        blobs_left=metric(result.blobs_left),
    )
    return Response(status_code=204)


@router.get(
    "/documents/{document_id}/file",
    response_class=FileResponse,
    responses={
        200: {
            "description": "The original file, as an attachment, with its own media type",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        },
        404: {"description": "No such document"},
        500: {"description": "The stored copy is missing, or two files claim its hash"},
    },
)
async def download_document(
    request: Request,
    document_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
) -> FileResponse:
    """The document's current bytes, exactly as imported, saved under the name of its first import.
    Always an attachment, so the browser saves it and never shows it as a page of this origin."""
    engine: Engine | None = request.app.state.engine
    store: BlobStore | None = request.app.state.blobs
    if engine is None or store is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    original = await run_in_threadpool(get_original, engine, document_id)
    if original is None:
        raise HTTPException(status_code=404, detail="no such document")
    try:
        path = await run_in_threadpool(store.find, original.sha256)
    except BlobIntegrityError:
        path = None
    if path is None:
        # The row names bytes the store cannot hand out: missing, or two files under one hash. The
        # id came in the request, so it is not logged (identifier() is for ids the app made); the
        # integrity scan finds which blob it is.
        log_event(logger, "documents.blob_unavailable")
        raise HTTPException(
            status_code=500, detail="the stored copy of this document is unavailable"
        )
    return FileResponse(
        path,
        filename=original.filename,
        content_disposition_type="attachment",
        # A header, not media_type: Starlette adds "; charset=utf-8" to a text/ media type, and a
        # text file may be UTF-16 or UTF-32 with a byte order mark (vault.ingest.validate).
        headers={"Content-Type": original.mime},
    )


class Heading(BaseModel):
    level: int  # 1 is the top
    text: str
    offset: int  # where the heading starts in its page's text, in characters (code points)


class Page(BaseModel):
    number: int  # from 1
    text: str
    headings: list[Heading]


class DocumentPages(BaseModel):
    version_id: str
    extracted: bool  # false until the extraction has stored the version's pages
    extractor: str | None
    total: int  # pages in the version, 0 until it is extracted
    offset: int
    limit: int
    pages: list[Page]
    next_offset: int | None  # where the next window starts, while pages remain after this one


@router.get(
    "/documents/{document_id}/pages",
    responses={
        404: {"description": "No such document"},
        500: {"description": "The stored text of this document breaks the page rules"},
    },
)
async def read_document_pages(
    request: Request,
    document_id: str,
    _user: Annotated[LocalUser, Depends(current_user)],
    limit: Annotated[int, Query(ge=1, le=MAX_WINDOW)] = DEFAULT_WINDOW,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> DocumentPages:
    """The extracted text of the document's current version (in use or in the trash, like the
    details), as plain text by page, with each page's headings. A window starts after ``offset``
    pages and holds at most ``limit`` pages and about a million characters: a page that would pass
    that is left for the next window, but the first page of a window is always sent whole.
    ``next_offset`` says where the next window starts. A document whose text has not been
    extracted yet answers with ``extracted`` false and no pages, not a 404."""
    engine = _engine(request)
    try:
        window = await run_in_threadpool(
            read_current_pages, engine, document_id, offset, limit, MAX_WINDOW_CHARS
        )
    except PageDataError:
        # The message names a page by number only, and the id came in the request, so neither is
        # logged (identifier() is for ids the app made).
        log_event(logger, "documents.pages_invalid")
        raise HTTPException(
            status_code=500, detail="the extracted text of this document is damaged"
        ) from None
    if window is None:
        raise HTTPException(status_code=404, detail="no such document")
    end = offset + len(window.pages)
    return DocumentPages(
        version_id=window.version_id,
        extracted=window.extractor is not None,
        extractor=window.extractor,
        total=window.total,
        offset=offset,
        limit=limit,
        pages=[
            Page(
                number=page.number,
                text=page.text,
                headings=[
                    Heading(level=h.level, text=h.title, offset=h.start) for h in page.headings
                ],
            )
            for page in window.pages
        ],
        next_offset=end if window.pages and end < window.total else None,
    )
