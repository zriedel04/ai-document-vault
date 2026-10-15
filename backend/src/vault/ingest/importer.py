"""Import one file: validate it, store it, and record it (Tier A of ``docs/architecture.md``
section 7).

1. The file is validated where it is, so an obviously wrong file is refused before it is copied.
2. It is copied into the blob store (hashed on the way), which keeps the same bytes once.
3. The stored copy is validated again. The source can change between the first check and the
   copy, but the blob cannot (it is read-only and named by its hash), so this check is the one
   that counts. A blob this import created for bytes that fail it is removed again.
4. In one transaction: the blob row, then either a new document with its first version, its
   import source and its first job (``extract``), or, when a document already holds these bytes,
   only a new import source for that document. Importing the same file twice gives one document
   with two sources. A document in the trash that is imported again comes out of the trash, with
   the state it had before: the import is the owner saying they want it.

Two imports of the same bytes at the same moment must not both create a document. The
transaction's first statement writes (the blob row), which takes SQLite's write lock, so the
second import waits and then finds the first one's document.

Nothing here logs: the caller logs the outcome with ids and counts. The file's name, path and
folders go into ``import_sources`` and nowhere else.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from sqlalchemy import Engine, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from vault.db import ids
from vault.db.schema import (
    IMPORT_METHODS,
    TRASHED,
    blobs,
    document_versions,
    documents,
    import_sources,
)
from vault.ingest.validate import DEFAULT_LIMITS, Limits, Reason, validate_file
from vault.jobs.queue import JobQueue, utc_now
from vault.storage.blobs import BlobStore, BlobTooLargeError

FIRST_STAGE: Final = "extract"


class Outcome(StrEnum):
    IMPORTED = "imported"  # a new document
    DUPLICATE = "duplicate"  # a new source for the document that already holds these bytes
    REFUSED = "refused"  # nothing was recorded


@dataclass(frozen=True)
class ImportRequest:
    """One file to import.

    ``path`` is the file to read; its extension is the one the file claims (an upload is first
    written to a temporary file with the original name's extension). The other fields describe
    where it came from and are stored as given: ``original_path`` is None for a browser upload,
    ``folder_segments`` are the folder names above the file, outermost first.
    """

    path: Path
    original_filename: str
    method: str
    batch_id: str
    original_path: str | None = None
    folder_segments: Sequence[str] = ()
    original_mtime: datetime | None = None


@dataclass(frozen=True)
class ImportResult:
    outcome: Outcome
    document_id: str | None = None
    reason: Reason | None = None


def _check(request: ImportRequest) -> None:
    """Refuse a request the ``import_sources`` table would refuse, before the file is copied: a
    request that failed only at the end would leave a blob that no row uses."""
    if request.method not in IMPORT_METHODS:
        msg = "method must be one of the import methods"
        raise ValueError(msg)
    if not ids.is_ulid(request.batch_id):
        msg = "batch_id is a ULID"
        raise ValueError(msg)
    if not request.original_filename:
        msg = "original_filename is required"
        raise ValueError(msg)
    if request.original_path is not None and not request.original_path:
        msg = "original_path is None or a path"
        raise ValueError(msg)
    if isinstance(request.folder_segments, str) or not all(
        isinstance(segment, str) for segment in request.folder_segments
    ):
        msg = "folder_segments is a list of folder names"
        raise ValueError(msg)


class Importer:
    def __init__(
        self,
        engine: Engine,
        store: BlobStore,
        *,
        limits: Limits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._engine = engine
        self._store = store
        self._limits = limits
        self._clock = clock
        self._queue = JobQueue(engine, clock=clock)

    @property
    def limits(self) -> Limits:
        return self._limits

    def import_file(self, request: ImportRequest) -> ImportResult:
        """Import ``request.path``. A file that is not acceptable is a REFUSED result with the
        reason, not an exception; a damaged or busy blob store raises its own ``BlobError``."""
        _check(request)
        first = validate_file(request.path, self._limits)
        if not first.ok or first.extension is None:  # an accepted verdict names its extension
            return ImportResult(Outcome.REFUSED, reason=first.reason)
        try:
            stored = self._store.put_file(
                request.path, first.extension, max_bytes=self._limits.max_bytes
            )
        except BlobTooLargeError:
            return ImportResult(Outcome.REFUSED, reason=Reason.TOO_LARGE)
        except FileNotFoundError:
            return ImportResult(Outcome.REFUSED, reason=Reason.NOT_A_FILE)
        except PermissionError:
            return ImportResult(Outcome.REFUSED, reason=Reason.UNREADABLE)
        verdict = validate_file(stored.path, self._limits)
        if not verdict.ok or verdict.extension is None or verdict.mime is None:
            if stored.created:
                self._forget_unused_blob(stored.sha256)
            return ImportResult(Outcome.REFUSED, reason=verdict.reason)
        return self._record(request, stored.sha256, stored.size, verdict.extension, verdict.mime)

    def _forget_unused_blob(self, sha256: str) -> None:
        """Remove a blob this import created, unless a row already uses it."""
        with self._engine.connect() as connection:
            used = connection.execute(
                select(blobs.c.sha256).where(blobs.c.sha256 == sha256)
            ).first()
        if used is None:
            self._store.delete(sha256)

    def _record(
        self, request: ImportRequest, sha256: str, size: int, extension: str, mime: str
    ) -> ImportResult:
        now = self._clock()
        with self._engine.begin() as connection:
            # The first statement writes, so this transaction holds the write lock from here on.
            connection.execute(
                sqlite_insert(blobs)
                .values(sha256=sha256, size=size, ext=extension, mime=mime, created_at=now)
                .on_conflict_do_nothing(index_elements=["sha256"])
            )
            existing = connection.execute(
                select(documents.c.id)
                .join(document_versions, documents.c.current_version_id == document_versions.c.id)
                .where(document_versions.c.blob_sha256 == sha256)
                .order_by(documents.c.created_at, documents.c.id)
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                connection.execute(
                    update(documents)
                    .where(documents.c.id == existing, documents.c.state == TRASHED)
                    .values(
                        state=documents.c.state_before_trash,
                        trashed_at=None,
                        state_before_trash=None,
                    )
                )
                document_id: str = existing
                outcome = Outcome.DUPLICATE
            else:
                document_id = ids.new_ulid()
                version_id = ids.new_ulid()
                connection.execute(
                    documents.insert().values(
                        id=document_id, state="processing", local_only=False, created_at=now
                    )
                )
                connection.execute(
                    document_versions.insert().values(
                        id=version_id,
                        document_id=document_id,
                        blob_sha256=sha256,
                        version_no=1,
                        origin="import",
                        created_at=now,
                    )
                )
                connection.execute(
                    update(documents)
                    .where(documents.c.id == document_id)
                    .values(current_version_id=version_id)
                )
                self._queue.enqueue(document_id, FIRST_STAGE, sha256, connection=connection)
                outcome = Outcome.IMPORTED
            connection.execute(
                import_sources.insert().values(
                    id=ids.new_ulid(),
                    document_id=document_id,
                    original_path=request.original_path,
                    original_filename=request.original_filename,
                    folder_segments=json.dumps(list(request.folder_segments), ensure_ascii=False),
                    original_mtime=request.original_mtime,
                    import_batch_id=request.batch_id,
                    method=request.method,
                    imported_at=now,
                )
            )
        return ImportResult(outcome, document_id=document_id)
