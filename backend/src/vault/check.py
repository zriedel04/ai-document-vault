"""The integrity check: ``python -m vault.check`` (``vault.ps1 check``, ``docs/architecture.md``
section 17).

It runs SQLite's own checks (``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check``) on
``vault.db`` and, if it exists, ``index.db``; re-hashes every blob (``BlobStore.verify``); reports
what the store's scan finds that is not a blob (links, hard links, one hash under several names,
anything else out of place); and compares the database with the store: versions whose blob is
missing, blob rows with no file that no version uses, blobs no row records (the start-up sweep's
rule, ``vault.ingest.orphans.unrecorded``) and blobs with a row that no version uses.

It only reads. The databases are opened read-only (``mode=ro``), nothing is migrated, sealed,
moved or deleted, and finding damage is all it does: repairing it is a restore from backup or the
owner's decision. (When a WAL database has no side files, a reader creates them, as any SQLite
reader does: an empty ``-wal`` and the ``-shm`` shared-memory index. Neither holds database pages,
and the database file itself is not written.)

It can run while the server runs. Under WAL a reader sees one committed snapshot and does not
block the server, and blobs are only read. The store and the database cannot be read at the same
instant, though, so every difference between them, and every odd entry in the store, is looked at
once more after the first pass, and one that has gone (an import or a removal that finished in
between) is not reported. An import that is still copying when the check ends can still show as a
blob without a row; run the check again, or with the server stopped, to tell that from damage.

The report holds counts, the kind of each problem and, for blob problems, the content hash, which
says nothing about the document. It never prints a file name, a path, a document name or anything
read from a document, and every message is fixed text (lesson 8 in ``CLAUDE.md``). Exit codes: 0
when nothing is wrong, 1 when a problem was found, 2 when the check could not run.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TextIO

from sqlalchemy import Connection, create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from vault.core.config import load_settings
from vault.db.schema import document_versions
from vault.ingest.orphans import recorded_hashes, unrecorded
from vault.storage.blobs import (
    BLOB_NAME,
    BlobError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobStore,
)
from vault.storage.layout import VaultPathError, VaultPaths, validate_vault_root

OK: Final = 0
PROBLEMS: Final = 1
CANNOT_RUN: Final = 2
HASHES_SHOWN: Final = 20  # per kind; the rest are counted

# Every problem the check can report, in report order, with its fixed description. A count is of
# entries, rows or blobs as the description says.
KINDS: Final = {
    "vault_db_missing": "vault.db is missing",
    "vault_db_unreadable": "vault.db cannot be read as a SQLite database",
    "vault_db_integrity": "errors SQLite's integrity check reports in vault.db",
    "vault_db_foreign_keys": "rows in vault.db whose foreign key points at no row",
    "vault_db_tables": "vault.db's document tables could not be read (an old or damaged schema)",
    "index_db_unreadable": "index.db cannot be read as a SQLite database",
    "index_db_integrity": "errors SQLite's integrity check reports in index.db",
    "index_db_foreign_keys": "rows in index.db whose foreign key points at no row",
    "blob_mismatch": "blobs whose bytes no longer match their hash",
    "blob_unreadable": "blobs that could not be read",
    "stray_link": "links (symbolic links, junctions, reparse points) in the blob store",
    "stray_hard_link": "files in a blob's place that also have another name (hard links)",
    "stray_duplicate": "hashes stored under more than one name",
    "stray_other": "other entries in the blob store that are not blobs",
    "version_blob_missing": "document versions whose blob is not in the store",
    "row_blob_missing": "blob rows that no version uses and whose file is not in the store",
    "blob_unrecorded": "blobs that no blob row records (the next start moves them to orphans)",
    "blob_unused": "blobs with a row that no document version uses",
}


class CheckError(Exception):
    """The check could not run. The message is fixed text, safe to print."""


@dataclass
class Report:
    """What the check found: a count per problem kind, and the hashes of blob problems."""

    blobs_checked: int = 0
    versions: int = 0
    notes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    hashes: dict[str, set[str]] = field(default_factory=dict)

    def add(self, kind: str, count: int = 1, sha256: str | None = None) -> None:
        if kind not in KINDS:  # pragma: no cover - a programming error, not an input
            msg = "unknown problem kind"
            raise ValueError(msg)
        if count <= 0:
            return
        self.counts[kind] = self.counts.get(kind, 0) + count
        if sha256 is not None:
            self.hashes.setdefault(kind, set()).add(sha256)

    @property
    def problems(self) -> int:
        return sum(self.counts.values())

    def lines(self) -> list[str]:
        out = ["Integrity check (read-only: nothing is repaired, moved or deleted)", *self.notes]
        out.append(f"Blobs re-hashed: {self.blobs_checked}; document versions: {self.versions}")
        if not self.counts:
            out.append("No problems found.")
            return out
        out.append(f"Problems found: {self.problems}")
        for kind, description in KINDS.items():
            count = self.counts.get(kind, 0)
            if not count:
                continue
            out.append(f"  {count} {description} [{kind}]")
            hashes = sorted(self.hashes.get(kind, ()))
            out.extend(f"      sha256 {sha256}" for sha256 in hashes[:HASHES_SHOWN])
            if len(hashes) > HASHES_SHOWN:
                out.append(f"      and {len(hashes) - HASHES_SHOWN} more")
        return out


@contextmanager
def _read_only(db_file: Path) -> Iterator[Connection]:
    """A read-only connection. ``mode=ro`` makes SQLite refuse any write, and a missing file is an
    error instead of a new empty database. The path goes into a ``file:`` URI with its special
    characters percent-encoded (``Path.as_uri``)."""
    uri = f"{db_file.as_uri()}?mode=ro"
    engine = create_engine(
        "sqlite+pysqlite://",
        creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
        poolclass=NullPool,
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def _sqlite_checks(report: Report, db_file: Path, prefix: str) -> bool:
    """Run SQLite's integrity and foreign key checks; False if the file cannot be read at all.
    The rows SQLite returns name tables, indexes and pages, but they are only counted."""
    try:
        with _read_only(db_file) as connection:
            integrity = [row[0] for row in connection.exec_driver_sql("PRAGMA integrity_check")]
            foreign = list(connection.exec_driver_sql("PRAGMA foreign_key_check"))
    except (SQLAlchemyError, sqlite3.Error):
        report.add(f"{prefix}_unreadable")
        return False
    report.add(f"{prefix}_integrity", 0 if integrity == ["ok"] else len(integrity))
    report.add(f"{prefix}_foreign_keys", len(foreign))
    return True


@dataclass(frozen=True)
class _Snapshot:
    recorded: set[str]  # the hash of every blob row
    used: set[str]  # the hash of every blob a version uses
    versions_by_blob: dict[str, int]  # how many versions use each blob


def _read_database(db_file: Path) -> _Snapshot:
    with _read_only(db_file) as connection:
        connection.exec_driver_sql("BEGIN")  # one snapshot for both reads
        try:
            recorded = recorded_hashes(connection)
            column = document_versions.c.blob_sha256
            counts: dict[str, int] = {
                str(sha256): int(count)
                for sha256, count in connection.execute(
                    select(column, func.count()).group_by(column)
                )
            }
        finally:
            connection.rollback()
    return _Snapshot(recorded=recorded, used=set(counts), versions_by_blob=counts)


def _still_there(store: BlobStore, sha256: str) -> bool:
    """Is a blob (or damage that holds the hash, reported on its own) stored under this hash?"""
    try:
        return store.find(sha256) is not None
    except BlobIntegrityError:
        return True


def _check_store_and_database(report: Report, paths: VaultPaths, database_ok: bool) -> list[str]:
    """Scan the store, report its odd entries and compare it with the database. Returns the
    hashes to re-hash."""
    store = BlobStore(paths.blobs)
    scan = store.scan()
    present = {sha256 for sha256, _ in scan.blobs}
    snapshot: _Snapshot | None = None
    if database_ok:
        try:
            snapshot = _read_database(paths.db)
        except (SQLAlchemyError, sqlite3.Error):
            report.add("vault_db_tables")
    # Looked at again now: an import may have finished publishing an entry since the scan.
    duplicates: set[str] = set()
    held: set[str] = set()  # hashes whose file is there, under a doubled name or a hard link
    for path in scan.strays:
        kind = store.stray_kind(path)
        match = BLOB_NAME.fullmatch(path.name)
        if kind == "duplicate" and match:
            duplicates.add(match.group(1))  # one problem per hash, however many names it has
        elif kind == "hard_link" and match:
            report.add("stray_hard_link", sha256=match.group(1))
        elif kind is not None:
            report.add(f"stray_{kind}")
        if kind in ("duplicate", "hard_link") and match:
            held.add(match.group(1))
    for sha256 in sorted(duplicates):
        report.add("stray_duplicate", sha256=sha256)
    rehash = [sha256 for sha256, _ in scan.blobs]
    if snapshot is None:
        report.notes.append("The database and the blob store were not compared.")
        return rehash
    present |= held  # reported once, as the stray it is, not again as a missing blob
    report.versions = sum(snapshot.versions_by_blob.values())
    missing = [h for h in sorted(snapshot.recorded | snapshot.used) if h not in present]
    for sha256 in missing:
        if _still_there(store, sha256):
            continue  # published after the scan
        if sha256 in snapshot.used:
            report.add("version_blob_missing", snapshot.versions_by_blob[sha256], sha256)
        else:
            report.add("row_blob_missing", sha256=sha256)
    unrecorded_now = unrecorded(scan, snapshot.recorded)
    unused_now = [h for h in sorted(snapshot.recorded - snapshot.used) if h in present]
    if unrecorded_now or unused_now:
        # Read the database again: an import may have written its rows since.
        try:
            fresh = _read_database(paths.db)
        except (SQLAlchemyError, sqlite3.Error):
            fresh = snapshot
        for sha256 in unrecorded_now:
            if sha256 not in fresh.recorded and _still_there(store, sha256):
                report.add("blob_unrecorded", sha256=sha256)
        for sha256 in unused_now:
            if sha256 not in fresh.used and _still_there(store, sha256):
                report.add("blob_unused", sha256=sha256)
    return rehash


def _rehash(report: Report, store: BlobStore, hashes: Iterable[str]) -> None:
    for sha256 in hashes:
        try:
            matches = store.verify(sha256)
        except BlobNotFoundError:
            continue  # removed since the scan
        except (OSError, BlobError):
            # Includes a hash that gained a second name since the scan: not a readable blob.
            report.add("blob_unreadable", sha256=sha256)
            continue
        report.blobs_checked += 1
        if not matches:
            report.add("blob_mismatch", sha256=sha256)


def check_vault(root: Path) -> Report:
    """Check the vault folder ``root`` (an existing folder) and return what was found."""
    paths = VaultPaths(root)
    report = Report()
    database_ok = False
    if not paths.db.is_file():
        report.add("vault_db_missing")
        report.notes.append("vault.db: missing")
    else:
        database_ok = _sqlite_checks(report, paths.db, "vault_db")
        report.notes.append(f"vault.db: {'checked' if database_ok else 'could not be read'}")
    if paths.index_db.is_file():
        index_ok = _sqlite_checks(report, paths.index_db, "index_db")
        report.notes.append(f"index.db: {'checked' if index_ok else 'could not be read'}")
    else:
        report.notes.append("index.db: not present (nothing to check)")
    to_rehash = _check_store_and_database(report, paths, database_ok)
    _rehash(report, BlobStore(paths.blobs), to_rehash)
    return report


def _vault_root() -> Path:
    try:
        settings = load_settings()
    except Exception as error:  # the message of a settings error can quote the file's path
        msg = "the settings could not be read; check backend\\config.toml or AIVAULT_CONFIG"
        raise CheckError(msg) from error
    if settings.vault.path is None:
        msg = "no vault folder is set (vault.path in the settings)"
        raise CheckError(msg)
    try:
        root = validate_vault_root(settings.vault.path)
    except VaultPathError as error:
        raise CheckError(str(error)) from error  # fixed text, owned by this package
    if not root.is_dir():
        msg = "the vault folder does not exist"
        raise CheckError(msg)
    return root


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    stdout = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print("usage: python -m vault.check  (takes no arguments)", file=sys.stderr)
        return CANNOT_RUN
    try:
        report = check_vault(_vault_root())
    except CheckError as error:
        print(f"vault check: {error}", file=sys.stderr)
        return CANNOT_RUN
    except Exception as error:  # a foreign message can quote a path: print only the class
        print(f"vault check: stopped by an unexpected {type(error).__name__}", file=sys.stderr)
        return CANNOT_RUN
    for line in report.lines():
        print(line, file=stdout)
    return PROBLEMS if report.problems else OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
