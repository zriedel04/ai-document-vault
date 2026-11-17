"""Permanent delete of a document in the trash (``docs/architecture.md`` sections 12 and 13).

Three steps, in this order:

1. One transaction removes the document and every row that belongs to it, and the ``blobs`` row of
   each of its blobs that no remaining version uses (``vault.db.documents.delete_permanently``).
   Only a document in the trash is deleted; any other is refused and nothing changes.
2. After the commit, each of those blobs is removed from the store (``remove_unused_blob``).
3. Then the document's vectors are removed from ``index.db``, if that file exists
   (``vault.retrieval.vectors.remove_document``), whatever model built it.

**The crash window.** If the process dies after step 1 commits and before step 2 has removed a
blob, the file stays in the store with no ``blobs`` row. That is exactly the case the start-up
sweep handles (``vault.ingest.orphans``): at the next start, before any request, the file is moved
unchanged to ``blobs/orphans/``. It is not deleted there, because the sweep cannot tell such a
file from an import that crashed before its rows were written, and originals are never deleted on
a guess. So after such a crash the deleted bytes stay in ``blobs/orphans/`` until the owner
empties that folder. A step 2 that fails (another program holds the file on Windows) leaves the
same state, with the same outcome at the next start.

Step 1 removes the document's chunks too, and with them their rows in the keyword index
``chunks_fts`` (a trigger on deleting a chunk, in the same transaction), so a search no longer
finds the document's text once the delete has committed. The plan's cached AI results do not exist
yet. A table in ``vault.db`` with a foreign key to a document or a version must be added to
``vault.db.documents.DOCUMENT_ROWS`` (a test fails until it is).

**Vectors.** ``index.db`` is another file, so its step cannot share the transaction of step 1. It
runs after the commit, so a refused delete never touches a live document's vectors. If it fails
(the index is busy, a link stands where it belongs) or the process dies first, the vectors stay
until the next start, where ``vault.retrieval.vectors.sweep`` removes every vector whose chunk is no
longer in ``vault.db``. Chunk ids are never reused, so a stale vector never stands for another chunk
in between, and every search result is a chunk id that the caller reads from ``vault.db``.

**Races.** Between the commit and step 2 an import of the same bytes can record the blob again,
and its file must then stay. Step 2 therefore decides again for each blob, inside a transaction
that holds the database's write lock (``write_transaction``) while it checks that no row names the
hash and removes the file under the store's claim on the hash. The importer, in turn, checks that
the blob's file still exists inside its own transaction, after its first write has taken the same
lock, and copies the file again if it is gone (``vault.ingest.importer``). The two checks are made
under one lock, so either the import's row is committed first and the blob stays, or the file is
removed first and the import stores it again. A restore or an import that lifts the document out
of the trash is serialised with step 1 by the same lock: it either comes first (the delete is then
refused) or finds no document.

Nothing here logs; the API logs counts only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from vault.db.documents import DeleteRefusal, blob_recorded, delete_permanently
from vault.db.engine import write_transaction
from vault.retrieval import vectors
from vault.storage.blobs import BlobError, BlobStore


@dataclass(frozen=True)
class Purged:
    """A document that was deleted. ``blobs_removed`` files went from the store; ``blobs_kept``
    were recorded again by an import in between, and ``blobs_left`` could not be removed now (the
    start-up sweep moves them to ``blobs/orphans/``). ``vectors_removed`` vectors went from
    ``index.db``; ``vectors_left`` is True if that step failed (the start-up sweep removes them)."""

    blobs_removed: int
    blobs_kept: int
    blobs_left: int
    vectors_removed: int = 0
    vectors_left: bool = False


def remove_unused_blob(engine: Engine, store: BlobStore, sha256: str) -> bool:
    """Remove the blob's file if no ``blobs`` row names it, deciding under the write lock.

    True if the file was removed (or was already gone), False if a row names the hash again, so
    the file is kept. Store and file system errors propagate.
    """
    with write_transaction(engine) as connection:
        if blob_recorded(connection, sha256):
            return False
        # Still under the write lock: no import can record the hash until the file is gone.
        store.delete(sha256)
        return True


def purge_document(
    engine: Engine, store: BlobStore, document_id: str, *, index_db: Path
) -> Purged | DeleteRefusal:
    """Delete a document in the trash for good: its rows, then each blob no row uses any more,
    then its vectors in ``index_db`` if that file exists."""
    deleted = delete_permanently(engine, document_id)
    if isinstance(deleted, DeleteRefusal):
        return deleted
    removed = kept = left = 0
    for sha256 in deleted.blobs:
        try:
            if remove_unused_blob(engine, store, sha256):
                removed += 1
            else:
                kept += 1
        except (OSError, BlobError):
            left += 1
    vectors_removed = 0
    vectors_left = False
    try:
        vectors_removed = vectors.remove_document(index_db, document_id)
    except (OSError, sqlite3.Error, vectors.VectorIndexError):
        vectors_left = True
    return Purged(
        blobs_removed=removed,
        blobs_kept=kept,
        blobs_left=left,
        vectors_removed=vectors_removed,
        vectors_left=vectors_left,
    )
