"""Start-up sweep for blobs that no database row uses.

The importer copies a file into the blob store and then records it in one transaction. A crash or a
database error between the two leaves a blob with no ``blobs`` row: nothing points at it, and an
import of the same bytes later would reuse it without knowing where it came from. At start-up,
before the API serves a request (so no import is under way), every blob in the store is compared
with the ``blobs`` table by its hash, the key both share, never by its file name or extension, and
each one without a row is moved to ``blobs/orphans/`` (``BlobStore.set_aside``). Nothing is
deleted: original files are irreplaceable, and a wrong verdict is undone by moving the file back.

Entries the store's scan does not count as blobs (a link, a junction, a folder in a blob's place,
one hash under two names) are only counted, never moved: they are damage for a person to look at.
A blob that cannot be moved is counted and left where it is, so one bad file does not stop the
vault from starting. The result is three counts; no hash or name is logged.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, Engine, select

from vault.db.schema import blobs
from vault.storage.blobs import BlobError, BlobStore, Scan


@dataclass(frozen=True)
class OrphanSweep:
    moved: int  # blobs without a row, now in blobs/orphans/
    failed: int  # blobs without a row that could not be moved (still in the store)
    strays: int  # entries that are not blobs, left alone and reported


def recorded_hashes(connection: Connection) -> set[str]:
    """The hash of every ``blobs`` row."""
    return set(connection.execute(select(blobs.c.sha256)).scalars())


def unrecorded(scan: Scan, recorded: set[str]) -> list[str]:
    """The hashes of the scanned blobs that no row records, compared by hash (the key the store
    and the table share), never by file name or extension. The integrity check uses it too."""
    return [sha256 for sha256, _ in scan.blobs if sha256 not in recorded]


def sweep_orphans(engine: Engine, store: BlobStore) -> OrphanSweep:
    """Move every blob that no ``blobs`` row names into ``blobs/orphans/``. Start-up only."""
    scan = store.scan()
    with engine.connect() as connection:
        recorded = recorded_hashes(connection)
    moved = failed = 0
    for sha256 in unrecorded(scan, recorded):
        try:
            store.set_aside(sha256)
        except (OSError, BlobError):
            failed += 1
        else:
            moved += 1
    return OrphanSweep(moved=moved, failed=failed, strays=len(scan.strays))
