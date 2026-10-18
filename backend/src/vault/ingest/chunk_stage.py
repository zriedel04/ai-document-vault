"""The chunk stage (``docs/architecture.md`` section 7, step 10): a version's stored pages become
its chunks and its rows in the keyword index, and the document becomes searchable.

A ``chunk`` job names a document and, as its input, the SHA-256 of the bytes whose pages it
chunks. The job works on the document's current version only if that version still holds those
bytes; a job for bytes the document no longer has, or for a document that was deleted, has
nothing to do and ends without changing anything (a newer version brings its own job).

1. The pages are read in one snapshot and chunked outside any lock: chunking is pure and can take
   a moment on a long document, and a writer waiting for SQLite's lock would wait as long.
2. Under the write lock, the version's pages are read again and compared with the ones chunked,
   headings included. Pages that changed in between (an extraction ran again) fail the attempt
   (``PagesChangedError``), so it runs again on the new pages, instead of storing chunks whose
   section paths come from headings that are gone. ``vault.db.chunks.write_chunks`` then
   replaces the version's chunks and checks them against the pages once more.
3. In the same transaction the document becomes ``searchable``: from ``processing`` or
   ``failed``, and from ``ready`` too, since its new chunks have no vectors yet. A document in the
   trash stays there, and ``searchable`` is the state a restore brings back. A version with no
   visible text has no chunks and is searchable all the same: nothing in it can be found, but
   every stage it needed has run.

The next stage, ``embed``, has no handler yet, so this stage queues nothing after it: a job that
nothing would ever claim would show the document as waiting for ever.

A version with no pages raises ``PagesMissingError``: the stage was queued before an extraction
stored them, and the attempt is retried after the queue's back-off. Nothing here logs; errors
carry fixed text only, since the pages are the document's own words.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy import Engine, case, select, update

from vault.db import ids
from vault.db.chunks import check_chunks, write_chunks
from vault.db.engine import read_snapshot, write_transaction
from vault.db.pages import StoredPages, pages_of
from vault.db.schema import TRASHED, document_versions, documents
from vault.ingest.chunking import structural_v1
from vault.jobs.queue import Job
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


def chunk_document(engine: Engine, document_id: str, sha256: str) -> Outcome:
    """Chunk the current version of the document if it holds the bytes ``sha256``."""
    if not ids.is_ulid(document_id):
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
    with read_snapshot(engine) as connection:
        version_id = connection.execute(current).scalar_one_or_none()
        stored = None if version_id is None else pages_of(connection, version_id)
    if version_id is None:
        return Outcome.STALE
    if stored is None:
        raise PagesMissingError
    chunked = check_chunks(structural_v1.chunk_pages(stored.pages), structural_v1.CHUNKER_VERSION)
    with write_transaction(engine) as connection:
        if connection.execute(current).scalar_one_or_none() != version_id:
            return Outcome.STALE
        if not _same(pages_of(connection, version_id), stored):
            raise PagesChangedError
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
    return Outcome.STORED


def _same(now: StoredPages | None, chunked: StoredPages) -> bool:
    return now is not None and now.pages == chunked.pages


def handler(engine: Engine) -> Handler:
    """The worker pool's handler for ``chunk`` jobs."""

    def run(job: Job) -> None:
        chunk_document(engine, job.document_id, job.input_hash)

    return run
