"""The chunk stage (``docs/architecture.md`` section 7, step 10): a version's stored pages become
its chunks and its rows in the keyword index, and the document becomes searchable.

A ``chunk`` job names a document and, as its input, the SHA-256 of the bytes whose pages it
chunks. The job works on the document's current version only if that version still holds those
bytes; a job for bytes the document no longer has, or for a document that was deleted, has
nothing to do and ends without changing anything (a newer version brings its own job).

1. The pages are read in one snapshot and chunked outside any lock: chunking is pure and can take
   a moment on a long document, and a writer waiting for SQLite's lock would wait as long. The
   snapshot only says what to chunk; it decides nothing.
2. Under the write lock (``BEGIN IMMEDIATE``), the document's current version and its pages are
   read again, and everything that ends the attempt is decided from them: a document that no
   longer holds these bytes ends the job as stale, a version with no pages fails the attempt
   (``PagesMissingError``), and pages or a version that changed since the snapshot (an extraction
   ran again, or the version moved away and back) are chunked again, up to ``MAX_ROUNDS`` times
   before the attempt fails (``PagesChangedError``), instead of storing chunks whose section
   paths come from headings that are gone. ``vault.db.chunks.write_chunks`` then replaces the
   version's chunks and checks them against the pages once more.
3. In the same transaction the document becomes ``searchable``, and the job is marked done or its
   failed attempt recorded (``vault.jobs.queue.Completion``). The decision and the job's end
   commit together: a crash cannot leave the document searchable behind a job still
   ``running``, and a change that commits before the decision is seen by it, while one that
   commits after it finds the job closed, so its ``enqueue`` adds a new job instead of returning
   this one. A worker that no longer holds the job gets ``JobLostError`` there and nothing is
   stored. The document becomes ``searchable`` from ``processing`` or ``failed``, and from
   ``ready`` too, since its new chunks have no vectors yet. A document in the trash stays there,
   and ``searchable`` is the state a restore brings back. A version with no visible text has no
   chunks and is searchable all the same: nothing in it can be found, but every stage it needed
   has run.

The next stage, ``embed``, has no handler yet, so this stage queues nothing after it: a job that
nothing would ever claim would show the document as waiting for ever.

A version with no pages fails the attempt with ``PagesMissingError``: the stage was queued before
an extraction stored them, and the attempt is retried after the queue's back-off. Nothing here
logs; errors carry fixed text only, since the pages are the document's own words.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy import Connection, Engine, case, select, update

from vault.db import ids
from vault.db.chunks import check_chunks, write_chunks
from vault.db.engine import read_snapshot, write_transaction
from vault.db.pages import pages_of
from vault.db.schema import TRASHED, document_versions, documents
from vault.ingest.chunking import structural_v1
from vault.jobs.queue import Completion, Job, JobQueue
from vault.jobs.worker import Handler

CHUNK_STAGE: Final = "chunk"
SEARCHABLE: Final = "searchable"


class ChunkStageError(Exception):
    """The attempt could not chunk the version; the queue retries it."""


class PagesMissingError(ChunkStageError):
    def __init__(self) -> None:
        super().__init__("The version has no stored pages to chunk yet.")


class PagesChangedError(ChunkStageError):
    def __init__(self) -> None:
        super().__init__("The version's pages changed while they were being chunked.")


class Outcome(StrEnum):
    STORED = "stored"  # the chunks were written and the document is searchable
    STALE = "stale"  # the document is gone or no longer holds these bytes
    # With a job only (without one these raise): the attempt failed and was recorded with the
    # queue in the transaction that found it, and the job is queued again or failed for good.
    RETRY = "retry"
    FAILED = "failed"


# Rounds of snapshot, chunk and check before pages that keep changing fail the attempt.
MAX_ROUNDS: Final = 3

_COMPLETION: Final = {
    Outcome.STORED: Completion.RECORDED,
    Outcome.STALE: Completion.RECORDED,
    Outcome.RETRY: Completion.ATTEMPT_QUEUED,
    Outcome.FAILED: Completion.ATTEMPT_FAILED,
}


def chunk_document(
    engine: Engine,
    document_id: str,
    sha256: str,
    *,
    finish: tuple[JobQueue, Job] | None = None,
) -> Outcome:
    """Chunk the current version of the document if it holds the bytes ``sha256``.

    Every decision that ends the attempt is taken under the write lock, from what the database
    holds then, never from the snapshot the chunks were made from: the snapshot only says what to
    chunk. With ``finish``, the job is completed (or its failed attempt recorded) in that same
    transaction, so a version change or an extraction that commits before it is seen, and one
    that commits after it finds the job no longer open and queues its own. Without ``finish`` a
    failed attempt raises (``PagesMissingError``, ``PagesChangedError``)."""
    if not ids.is_ulid(document_id):  # names no document, now or later
        if finish is not None:
            queue, job = finish
            queue.complete(job.id, job.worker_id)
        return Outcome.STALE
    current = (
        select(document_versions.c.id)
        .select_from(
            documents.join(
                document_versions,
                (document_versions.c.id == documents.c.current_version_id)
                & (document_versions.c.document_id == documents.c.id),
            )
        )
        .where(documents.c.id == document_id, document_versions.c.blob_sha256 == sha256)
    )
    for round_no in range(1, MAX_ROUNDS + 1):
        with read_snapshot(engine) as connection:
            version_id = connection.execute(current).scalar_one_or_none()
            stored = None if version_id is None else pages_of(connection, version_id)
        chunked = None
        if stored is not None:
            chunks_made = structural_v1.chunk_pages(stored.pages)
            chunked = check_chunks(chunks_made, structural_v1.CHUNKER_VERSION)
        with write_transaction(engine) as connection:
            now_version = connection.execute(current).scalar_one_or_none()
            if now_version is None:
                _finish(finish, connection)
                return Outcome.STALE
            now_pages = pages_of(connection, now_version)
            if now_pages is None:
                return _failed(finish, connection, PagesMissingError())
            if (
                now_version != version_id
                or stored is None
                or chunked is None
                or now_pages.pages != stored.pages
            ):
                # The version or its pages changed after the snapshot: chunk them again.
                if round_no == MAX_ROUNDS:
                    return _failed(finish, connection, PagesChangedError())
                continue
            write_chunks(connection, version_id, chunked, structural_v1.CHUNKER_VERSION)
            in_trash = documents.c.state == TRASHED
            connection.execute(
                update(documents)
                .where(documents.c.id == document_id)
                .values(
                    # SET reads the row as it was, so both columns see the same state.
                    state=case((in_trash, documents.c.state), else_=SEARCHABLE),
                    state_before_trash=case((in_trash, SEARCHABLE), else_=None),
                )
            )
            _finish(finish, connection)
            return Outcome.STORED
    raise AssertionError  # pragma: no cover - the last round always returns


def _finish(finish: tuple[JobQueue, Job] | None, connection: Connection) -> None:
    if finish is not None:
        queue, job = finish
        queue.complete(job.id, job.worker_id, connection=connection)


def _failed(
    finish: tuple[JobQueue, Job] | None, connection: Connection, error: ChunkStageError
) -> Outcome:
    """Record the failed attempt in the transaction that found it, or raise without a job."""
    if finish is None:
        raise error
    queue, job = finish
    status = queue.fail(job.id, job.worker_id, error, connection=connection)
    return Outcome.FAILED if status == "failed" else Outcome.RETRY


def handler(engine: Engine, queue: JobQueue) -> Handler:
    """The worker pool's handler for ``chunk`` jobs, which ends each job in ``queue`` in the
    transaction that decides its outcome."""

    def run(job: Job) -> Completion:
        outcome = chunk_document(engine, job.document_id, job.input_hash, finish=(queue, job))
        return _COMPLETION[outcome]

    return run
