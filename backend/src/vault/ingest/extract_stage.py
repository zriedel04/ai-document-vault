"""The extraction stage (``docs/architecture.md`` section 7, step 7): a version's blob becomes its
stored pages, in the sandbox, and the ``chunk`` stage is queued for them.

An ``extract`` job names a document and, as its input, the SHA-256 of the bytes it extracts (the
importer queues it). Like the chunk stage, it works on the document's current version only if that
version still holds those bytes; a job for bytes the document no longer has, or for a document that
was deleted, ends without changing anything.

1. The blob's type and size are read in one snapshot, which only says what to extract. The blob is
   read whole, and its length and SHA-256 are compared with the row and the job before anything
   runs on it (``BlobChangedError``): a blob edited in place must not be stored as the extraction
   of the bytes its name gives.
2. The extractor for the type (``EXTRACTORS``) runs in the sandbox (``vault.jobs.sandbox``) with
   the configured limits, the text extractors with the configured fallback code page. Nothing is
   locked meanwhile. Its result is checked before it is stored: the extractor and version the
   entry point writes, an encoding that extractor can report, no headings from an extractor that
   finds none (PDF), and the page rules of ``vault.db.pages`` (``ExtractionResultError``), because
   a child that a hostile file took over writes its own answer.
3. Under the write lock (``BEGIN IMMEDIATE``), the document's current version is read again. A
   document that no longer holds these bytes ends the job as stale. Otherwise the pages replace
   the version's pages (the pages come from the bytes alone, so they belong to whichever version
   holds them now), the ``chunk`` job for the same bytes is queued and the job is marked done, all
   in that transaction (``vault.jobs.queue.Completion``). The document's state is left alone: it
   becomes ``searchable`` when the chunk stage has run.

How each failure ends the attempt (ADR 0005: a failed job marks the document Failed once its
attempts are used up):

- every sandbox failure that the document can cause (``DOCUMENT_FAILURES``: a timeout, the memory
  cap, a crash, the extractor raising, an answer that cannot be read) and the checks above fail
  the attempt with the error's class name only, ``SandboxChildError`` for an extractor that
  raised, never the name the child reported or any message, since a hostile file controls both.
  The queue retries it after its back-off, within the job's attempts;
- a sandbox that cannot run at all (``SETUP_FAILURES``: an entry point the allowlist refuses, a
  child that cannot be started or limited on this system) and a type with no extractor are a
  failure of the setup, not a result about the document: the job is given back without using an
  attempt (``JobQueue.defer``) and runs again after ``SETUP_DELAY``, its error's class name showing
  why it waits; the document stays ``processing`` and is never failed by it.

The types extracted are TXT, Markdown, DOCX and PDF. A PDF is read by its text layer only
(``vault.ingest.extractors.pdf``, ``pdf-1``): a first version on ADR 0015, which stays Proposed
until the owner's PDF spike has run, so the extractor may still change or be replaced. A PDF page
with no text layer (a scan) is stored as an empty page and the document completes like any other:
the chunk stage makes it searchable with no chunks for those pages. Reading such pages is the OCR
stage's work (Phase 3, ADRs 0014 and 0015), which finds them by their empty text; nothing here
runs OCR. A PDF that needs a password, has no pages, is over the extractor's caps or cannot be
parsed fails the attempt as an extractor that raised (``SandboxChildError``).

A type with no extractor yet (PNG and JPEG, which wait on the OCR stage) is never claimed: the
worker pool claims ``extract`` jobs only for the types in ``EXTRACTORS`` (``EXTENSIONS``), so those
jobs wait in the queue, as a stage with no handler does, and run when their extractor arrives.

Nothing here logs: the worker logs the stage and the outcome. Errors carry fixed text only.
"""

from __future__ import annotations

import codecs
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from sqlalchemy import Connection, Engine, select

from vault.db import ids
from vault.db.engine import read_snapshot, write_transaction
from vault.db.pages import PageHeading, PageText, check_pages, write_pages
from vault.db.schema import blobs as blob_rows
from vault.db.schema import document_versions, documents
from vault.ingest.chunk_stage import CHUNK_STAGE
from vault.ingest.extractors import Extraction
from vault.ingest.extractors import text as text_extractor
from vault.ingest.validate import TEXT_BOMS
from vault.jobs import sandbox
from vault.jobs.queue import Completion, Job, JobQueue
from vault.jobs.sandbox import (
    SandboxChildError,
    SandboxCrashError,
    SandboxLimits,
    SandboxMemoryLimitError,
    SandboxRefusedError,
    SandboxResultError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from vault.jobs.worker import Handler
from vault.storage.blobs import BlobStore

EXTRACT_STAGE: Final = "extract"
# How long a job waits after a failure of the setup before it is tried again.
SETUP_DELAY: Final = timedelta(minutes=10)
# Rounds of snapshot and decision before a version that keeps appearing fails the attempt.
MAX_ROUNDS: Final = 2

# The sandbox failures a document can cause: the attempt fails and is retried within its attempts.
DOCUMENT_FAILURES: Final = (
    SandboxTimeoutError,
    SandboxMemoryLimitError,
    SandboxCrashError,
    SandboxChildError,
    SandboxResultError,
)
# The sandbox failures of the setup: the job is deferred, the document is never failed by them.
SETUP_FAILURES: Final = (SandboxRefusedError, SandboxUnavailableError)

# The encodings the text extractor reports without its fallback: UTF-8 and every byte order mark.
_TEXT_ENCODINGS: Final = frozenset(
    {"utf-8", *(codecs.lookup(encoding).name for _bom, encoding in TEXT_BOMS)}
)


@dataclass(frozen=True)
class Extractor:
    """How a blob type is extracted: the sandbox entry point, the extractor name and version its
    result must carry, whether it takes the fallback code page (and may report it), and whether it
    can find headings at all."""

    entry: str
    version: str
    text: bool
    headings: bool = True

    def encodings(self, fallback: str) -> frozenset[str | None]:
        """The encodings a result of this extractor may name."""
        if not self.text:
            return frozenset({None})
        return frozenset({*_TEXT_ENCODINGS, codecs.lookup(fallback).name})


# By the blob's canonical extension (``vault.ingest.validate.EXTENSIONS``). The DOCX and PDF
# versions are written out so the server never imports python-docx or loads PDFium; a test ties
# each to its extractor's VERSION. ``pdf-1`` finds no headings (pdfplumber's font sizes, in ADR
# 0015's proposal, are not used yet), so a PDF answer that holds one is refused.
EXTRACTORS: Final[Mapping[str, Extractor]] = MappingProxyType(
    {
        "txt": Extractor("text", text_extractor.VERSION, text=True),
        "md": Extractor("markdown", text_extractor.VERSION, text=True),
        "docx": Extractor("docx", "docx-1", text=False),
        "pdf": Extractor("pdf", "pdf-1", text=False, headings=False),
    }
)
EXTENSIONS: Final = frozenset(EXTRACTORS)

type Runner = Callable[..., object]


class ExtractStageError(Exception):
    """The attempt could not extract the version. Messages are fixed text."""


class BlobChangedError(ExtractStageError):
    def __init__(self) -> None:
        super().__init__("The stored blob does not hold the bytes its hash and size name.")


class ExtractionResultError(ExtractStageError):
    def __init__(self) -> None:
        super().__init__("The extractor's result is not one it can return.")


class NoExtractorError(ExtractStageError):
    def __init__(self) -> None:
        super().__init__("No extractor reads this type of file yet.")


class VersionChangedError(ExtractStageError):
    def __init__(self) -> None:
        super().__init__("The document's version changed while it was being extracted.")


class Outcome(StrEnum):
    STORED = "stored"  # the pages were written and the chunk job queued
    STALE = "stale"  # the document is gone or no longer holds these bytes
    # With a job only: the attempt failed and was recorded in the transaction that found it.
    RETRY = "retry"
    FAILED = "failed"


_COMPLETION: Final = {
    Outcome.STORED: Completion.RECORDED,
    Outcome.STALE: Completion.RECORDED,
    Outcome.RETRY: Completion.ATTEMPT_QUEUED,
    Outcome.FAILED: Completion.ATTEMPT_FAILED,
}


def extract_document(
    engine: Engine,
    store: BlobStore,
    document_id: str,
    sha256: str,
    *,
    limits: SandboxLimits,
    fallback: str = text_extractor.FALLBACK_ENCODING,
    finish: tuple[JobQueue, Job] | None = None,
    runner: Runner = sandbox.run,
) -> Outcome:
    """Extract the current version of the document if it holds the bytes ``sha256``.

    Raises a ``SandboxError`` subclass when the sandbox gives no result, ``NoExtractorError`` for
    a type ``EXTRACTORS`` does not name, and ``BlobChangedError`` or ``ExtractionResultError`` as
    the module docstring says. With ``finish``, the job is completed in the transaction that
    stores the pages; ``runner`` is the sandbox's ``run`` (tests replace it)."""
    if fallback not in text_extractor.FALLBACK_ENCODINGS:
        msg = "The fallback must be a Windows code page from FALLBACK_ENCODINGS."
        raise ValueError(msg)
    if not ids.is_ulid(document_id):  # names no document, now or later
        if finish is not None:
            queue, job = finish
            queue.complete(job.id, job.worker_id)
        return Outcome.STALE
    current = (
        select(document_versions.c.id, blob_rows.c.ext, blob_rows.c.size)
        .select_from(
            documents.join(
                document_versions,
                (document_versions.c.id == documents.c.current_version_id)
                & (document_versions.c.document_id == documents.c.id),
            ).join(blob_rows, blob_rows.c.sha256 == document_versions.c.blob_sha256)
        )
        .where(documents.c.id == document_id, document_versions.c.blob_sha256 == sha256)
    )
    for round_no in range(1, MAX_ROUNDS + 1):
        with read_snapshot(engine) as connection:
            found = connection.execute(current).first()
        extraction = None
        if found is not None:
            extractor = EXTRACTORS.get(found.ext)
            if extractor is None:
                raise NoExtractorError
            data = _read_blob(store, sha256, found.size)
            result = runner(
                extractor.entry,
                data,
                limits,
                option=fallback if extractor.text else None,
            )
            del data
            extraction = _checked(result, extractor, fallback)
        with write_transaction(engine) as connection:
            now = connection.execute(current).first()
            if now is None:
                _finish(finish, connection)
                return Outcome.STALE
            if extraction is None:
                # The version came back after the snapshot: extract it again.
                if round_no == MAX_ROUNDS:
                    return _failed(finish, connection, VersionChangedError())
                continue
            extractor_name, encoding, pages = extraction
            write_pages(connection, now.id, pages, extractor_name, encoding)
            queue = finish[0] if finish is not None else JobQueue(engine)
            queue.enqueue(document_id, CHUNK_STAGE, sha256, connection=connection)
            _finish(finish, connection)
            return Outcome.STORED
    raise AssertionError  # pragma: no cover - the last round always returns


def _read_blob(store: BlobStore, sha256: str, size: int) -> bytes:
    """The blob's bytes, read whole and compared with the size its row gives and its hash."""
    with store.open(sha256) as stream:
        data = stream.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise BlobChangedError
    return data


def _checked(
    result: object, extractor: Extractor, fallback: str
) -> tuple[str, str | None, tuple[PageText, ...]]:
    """The extractor name, encoding and pages of ``result``, if it is what ``extractor`` returns
    and its pages follow the page rules; ``ExtractionResultError`` otherwise."""
    if (
        not isinstance(result, Extraction)
        or result.extractor != extractor.version
        or result.encoding not in extractor.encodings(fallback)
        or (not extractor.headings and any(page.headings for page in result.pages))
    ):
        raise ExtractionResultError
    pages = tuple(
        PageText(
            page.number,
            page.text,
            tuple(PageHeading(h.level, h.title, h.start, h.end) for h in page.headings),
        )
        for page in result.pages
    )
    try:
        check_pages(pages)
    except ValueError:
        raise ExtractionResultError from None
    return result.extractor, result.encoding, pages


def _finish(finish: tuple[JobQueue, Job] | None, connection: Connection) -> None:
    if finish is not None:
        queue, job = finish
        queue.complete(job.id, job.worker_id, connection=connection)


def _failed(
    finish: tuple[JobQueue, Job] | None, connection: Connection, error: ExtractStageError
) -> Outcome:
    """Record the failed attempt in the transaction that found it, or raise without a job."""
    if finish is None:
        raise error
    queue, job = finish
    status = queue.fail(job.id, job.worker_id, error, connection=connection)
    return Outcome.FAILED if status == "failed" else Outcome.RETRY


def handler(
    engine: Engine,
    queue: JobQueue,
    store: BlobStore,
    limits: SandboxLimits,
    fallback: str = text_extractor.FALLBACK_ENCODING,
    *,
    runner: Runner = sandbox.run,
) -> Handler:
    """The worker pool's handler for ``extract`` jobs. Register it with ``EXTENSIONS`` as the
    stage's extensions, so that jobs for a type with no extractor wait in the queue.

    A ``fallback`` that is not one of ``FALLBACK_ENCODINGS`` is refused here, at start-up, and not
    by every job: a setting the code cannot use is not a failure of the documents."""
    if fallback not in text_extractor.FALLBACK_ENCODINGS:
        msg = "The fallback must be a Windows code page from FALLBACK_ENCODINGS."
        raise ValueError(msg)

    def run(job: Job) -> Completion:
        try:
            outcome = extract_document(
                engine,
                store,
                job.document_id,
                job.input_hash,
                limits=limits,
                fallback=fallback,
                finish=(queue, job),
                runner=runner,
            )
        except (*SETUP_FAILURES, NoExtractorError) as error:
            queue.defer(job.id, job.worker_id, error, SETUP_DELAY)
            return Completion.DEFERRED
        return _COMPLETION[outcome]

    return run
