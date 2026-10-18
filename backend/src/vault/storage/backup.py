"""Back up a vault into another folder (``docs/architecture.md``, section 17).

A backup target holds one shared blob store and one dated folder per snapshot::

    <target>/
      backup.lock                      held while a backup runs; never deleted
      blobs/sha256/aa/bb/<hash>.<ext>  every original file, copied once, read-only
      snapshots/2026-10-17T143005Z/
        vault.db                       consistent copy of the database, read-only
        config.toml                    the settings file, if there is one, read-only

The database is copied with SQLite's ``VACUUM INTO``, which reads one consistent snapshot even
while the app writes, into ``vault.db.partial``; the copy is checked (``PRAGMA quick_check``) and
lists the blobs it refers to. Each of those blobs not yet in the target is copied with the blob
store's own ``put``: to a temporary name in the target, hashed as it is written, flushed, and
published under its final name only if the hash is the one it is filed under, then marked
read-only. Only then is the snapshot renamed to ``vault.db``, so a snapshot folder with a
``vault.db`` is complete and every blob it names is in the target (except the ones the result counts
as problems). ``index.db``, ``derived/``, ``logs/``, ``backups/`` and the local token are never
copied: the first three are rebuilt by the app, and the token is a credential the server creates
again.

Nothing is ever overwritten: a snapshot folder is created only if its name is free, a published
file never replaces an entry, and a blob already in the target is kept (its size is compared with
the database's record; the full re-hash belongs to the integrity check and the restore). A run that
fails removes what it wrote into its own snapshot folder. A run that was killed leaves at most a
temporary file, which the next run removes, a blob that is not yet read-only, which the next run
seals, and a snapshot folder without ``vault.db``, which the next run counts and leaves for the
owner. Two runs on one target take turns through an operating-system lock on ``backup.lock``, which
is released when a process ends however it ends.

A symbolic link, a Windows junction or any other reparse point in the target path, or where a
folder of the backup belongs, is refused, as the blob store refuses one in the vault. Like the
store's, the check is made just before each level is used: it guards against a damaged or
mistyped target, not against another process racing it. A target inside the vault folder, one
that holds the vault folder, and one inside a Git working tree are refused too. Messages are fixed
text: no file name, hash or path appears in them.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final, cast

from vault.storage import acl
from vault.storage.blobs import (
    EXTENSION,
    READ_ONLY,
    SHA256,
    WRITABLE,
    BlobHashMismatchError,
    BlobIntegrityError,
    BlobStore,
    BlobTooLargeError,
    _is_link,
    _is_real_dir,
    _is_real_file,
    _lstat,
    publish_new,
)
from vault.storage.layout import VaultPaths, git_root_containing

LOCK_FILE: Final = "backup.lock"
BLOBS: Final = "blobs"
SNAPSHOTS: Final = "snapshots"
DATABASE: Final = "vault.db"
CONFIG: Final = "config.toml"
PARTIAL: Final = ".partial"
STAMP_FORMAT: Final = "%Y-%m-%dT%H%M%SZ"  # no colon: Windows does not allow one in a name
STAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z")  # always ``fullmatch``
SQLITE_TIMEOUT_SECONDS: Final = 30.0


class BackupError(Exception):
    """The backup cannot go on. The message is fixed text and names no file or folder."""


@dataclass(frozen=True)
class BackupResult:
    snapshot: str  # the snapshot folder's name, which this module built from the time
    copied: int  # blobs copied in this run
    present: int  # blobs that were already in the target
    missing: int  # blobs the snapshot lists that the vault's store does not hold
    damaged: int  # blobs whose bytes or record do not match their hash, not copied
    unreadable: int  # blobs that could not be read (another program may hold them), not copied
    mismatched: int  # blobs already in the target whose size differs from the record, kept
    incomplete: int  # earlier snapshot folders without a vault.db
    cleaned: int  # leftovers of an interrupted copy removed from the target, or blobs re-sealed
    private: bool | None  # Windows: is the target, all of it, owner-only? None elsewhere

    @property
    def problems(self) -> int:
        return self.missing + self.damaged + self.unreadable + self.mismatched


class _SourceReadError(Exception):
    """Reading a blob from the vault failed (as opposed to writing it into the target)."""


class _SourceReader:
    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        try:
            return self._stream.read(size)
        except OSError as error:
            raise _SourceReadError from error


def _within(path: Path, folder: Path) -> bool:
    return Path(os.path.normcase(path)).is_relative_to(Path(os.path.normcase(folder)))


def check_target(target: Path, vault_root: Path) -> Path:
    """The target as an absolute path, or ``BackupError``. Creates nothing.

    Every level of the path that exists, from the drive down, must be a real folder: a link or a
    junction could send the backup somewhere else than the folder the owner named, or into the
    vault itself. A target inside the vault or holding it (by the path given or the one it
    resolves to, as the vault folder may be a link the owner set up) or inside a Git working tree
    is refused.
    """
    if not target.is_absolute():
        msg = "the backup target must be an absolute path"
        raise BackupError(msg)
    target = Path(os.path.abspath(target))
    for level in (*reversed(target.parents), target):
        status = _lstat(level)
        if status is None:
            break  # this level and the ones below it are created later
        if _is_link(status):
            msg = "the backup target path goes through a link or a junction; name the real folder"
            raise BackupError(msg)
        if not stat.S_ISDIR(status.st_mode):
            msg = "the backup target path goes through something that is not a folder"
            raise BackupError(msg)
    # Both spellings of each: the path as given, and the one the system gives back for it (a
    # short 8.3 name or a subst drive can name the same folder another way).
    targets = {target, target.resolve()}
    roots = {Path(os.path.abspath(vault_root)), vault_root.resolve()}
    for spelling in targets:
        for root in roots:
            if _within(spelling, root):
                msg = "the backup target is inside the vault folder; choose a folder outside it"
                raise BackupError(msg)
            if _within(root, spelling):
                # The backup's blobs/ and snapshots/ would sit among the vault's own files.
                msg = "the vault folder is inside the backup target; choose a folder beside it"
                raise BackupError(msg)
    if git_root_containing(target) is not None:
        msg = "the backup target is inside a Git repository; choose a folder outside every one"
        raise BackupError(msg)
    return target


def _ensure_folder(folder: Path, *, parents: bool = False) -> bool:
    """Make the folder if it is missing (True if it was made); refuse anything but a real one."""
    created = False
    if _lstat(folder) is None:
        folder.mkdir(parents=parents, exist_ok=True)
        created = True
    if not _is_real_dir(_lstat(folder)):
        msg = "something that is not a folder (or a link) is where a folder of the backup belongs"
        raise BackupError(msg)
    return created


def _lock(fd: int, *, take: bool) -> None:
    """Take an exclusive lock on the open file without waiting (OSError if another process or
    handle holds it), or give it back. The end of a process gives it back too. Windows frees the
    lock of a closed handle only after a while, so it is given back before the file is closed."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)  # the lock covers the first byte, from the position
        msvcrt.locking(fd, msvcrt.LK_NBLCK if take else msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - not Windows
        import fcntl

        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if take else fcntl.LOCK_UN)


@contextmanager
def _target_lock(target: Path) -> Iterator[None]:
    path = target / LOCK_FILE
    status = _lstat(path)
    if status is not None and not _is_real_file(status):
        msg = "something that is not a file (or a link) is where the backup's lock file belongs"
        raise BackupError(msg)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            _lock(fd, take=True)
        except OSError:
            msg = "another backup to this target is still running; wait for it and try again"
            raise BackupError(msg) from None
        try:
            yield
        finally:
            _lock(fd, take=False)
    finally:
        os.close(fd)


def _fsync(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _snapshot(db: Path, out: Path) -> list[tuple[object, object, object]]:
    """Copy the database into ``out`` and return the ``blobs`` rows of the copy."""
    if not _is_real_file(_lstat(db)):
        msg = "the vault folder has no database yet; start the app once before backing it up"
        raise BackupError(msg)
    try:
        # mode=rw: never create an empty database where the vault's should be.
        source = sqlite3.connect(f"{db.as_uri()}?mode=rw", uri=True, timeout=SQLITE_TIMEOUT_SECONDS)
        try:
            source.execute("VACUUM INTO ?", (str(out),))
        finally:
            source.close()
        _fsync(out)
        # A plain path, not a URI: SQLite refuses a URI that names a network share
        # (file://nas/backups/...), and a backup folder on one is a fair choice. The copy is in
        # rollback-journal mode, so reading it creates no side file.
        copy = sqlite3.connect(str(out))
        try:
            check = copy.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                msg = "the database snapshot did not pass SQLite's check; nothing was published"
                raise BackupError(msg)
            rows = copy.execute("SELECT sha256, ext, size FROM blobs ORDER BY sha256").fetchall()
        finally:
            copy.close()
    except sqlite3.Error:
        msg = "SQLite could not take or read the database snapshot; nothing was published"
        raise BackupError(msg) from None
    return [(row[0], row[1], row[2]) for row in rows]


def _write_new(data: bytes, final: Path, created: list[Path]) -> None:
    """Write bytes to a temporary name, flush them and publish them under ``final`` only if that
    name is free, then make the file read-only."""
    temp = final.with_name(final.name + PARTIAL)
    with temp.open("xb") as out:
        created.append(temp)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    _publish(temp, final, created)


def _publish(temp: Path, final: Path, created: list[Path]) -> None:
    if not publish_new(temp, final):
        msg = "a file is already where the snapshot's file belongs; nothing was replaced"
        raise BackupError(msg)
    created.append(final)
    temp.unlink(missing_ok=True)  # first: Windows cannot remove a name of a read-only file
    final.chmod(READ_ONLY)


def _undo(folder: Path, created: list[Path]) -> None:
    """Remove what this run wrote into its own snapshot folder, after a failure."""
    for path in reversed(created):
        with suppress(OSError):
            if _is_real_file(_lstat(path)):
                path.chmod(stat.S_IMODE(os.lstat(path).st_mode) | WRITABLE)
                path.unlink()
    with suppress(OSError):
        folder.rmdir()


def _count_incomplete(snapshots: Path, mine: Path) -> int:
    return sum(
        1
        for entry in snapshots.iterdir()
        if entry != mine
        and STAMP.fullmatch(entry.name)
        and _is_real_dir(_lstat(entry))
        and not _is_real_file(_lstat(entry / DATABASE))
    )


def _secure_target(target: Path, created: bool) -> bool | None:
    """On Windows, make a new or empty target folder private to its owner as the vault is, and
    say whether the folder itself is private (what is below it is :func:`_tree_is_private`'s).
    Anything that fails (a drive that keeps no permissions, a folder with files that others may
    read) is reported, not fixed: the owner decides about their folder."""
    if not acl.IS_WINDOWS:
        return None
    try:
        acl.secure_vault_folder(target, created=created)
    except acl.VaultAccessError:
        return False
    return True


def _tree_is_private(target: Path) -> bool:
    """Is every file and folder below the target private to its owner? The vault's own start-up
    walk (``acl.verify_tree``): a folder or file moved in from elsewhere keeps its own access list,
    and new snapshots and blobs inherit the list of the folder they are written into, so a private
    target folder says nothing about ``blobs/``, ``snapshots/`` or what is in them. Run after the
    backup has written, so the answer covers this run's files too. Windows only."""
    try:
        acl.verify_tree(target)
    except acl.VaultAccessError:
        return False
    return True


@dataclass
class _Counts:
    copied: int = 0
    present: int = 0
    missing: int = 0
    damaged: int = 0
    unreadable: int = 0
    mismatched: int = 0


def _copy_blobs(
    rows: list[tuple[object, object, object]], source: BlobStore, target: BlobStore
) -> _Counts:
    counts = _Counts()
    for sha256, ext, size in rows:
        # The database has CHECK constraints on each, but a value read back is checked again.
        if not (
            isinstance(sha256, str)
            and SHA256.fullmatch(sha256)
            and isinstance(ext, str)
            and EXTENSION.fullmatch(ext)
            and type(size) is int
            and size >= 0
        ):
            counts.damaged += 1
            continue
        try:
            existing = target.find(sha256)
        except BlobIntegrityError:
            counts.mismatched += 1  # two names for one hash in the target
            continue
        if existing is not None:
            if os.lstat(existing).st_size == size:
                counts.present += 1
            else:
                counts.mismatched += 1
            continue
        try:
            found = source.find(sha256)
        except BlobIntegrityError:
            counts.damaged += 1
            continue
        if found is None:
            counts.missing += 1
            continue
        try:
            stream = found.open("rb")
        except OSError:
            counts.unreadable += 1
            continue
        try:
            with stream:
                stored = target.put(
                    cast("BinaryIO", _SourceReader(stream)),
                    ext,
                    max_bytes=size,
                    expected_sha256=sha256,
                )
        except _SourceReadError:
            counts.unreadable += 1
        except (BlobTooLargeError, BlobHashMismatchError):
            counts.damaged += 1
        else:
            # Not created only if another writer stored it first, which the target lock prevents.
            counts.copied += int(stored.created)
            counts.present += int(not stored.created)
    return counts


def run_backup(
    vault_root: Path,
    target: Path,
    *,
    config_file: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    secure: Callable[[Path, bool], bool | None] = _secure_target,
    tree: Callable[[Path], bool] = _tree_is_private,
) -> BackupResult:
    """Back up the vault at ``vault_root`` into ``target`` (see the module docstring).

    ``private`` in the result is True only if the target folder and everything below it, this
    run's files included, are private to the owner (Windows); one open entry anywhere makes it
    False. Elsewhere it is None."""
    target = check_target(target, vault_root)
    paths = VaultPaths(vault_root)
    created_target = _ensure_folder(target, parents=True)
    private = secure(target, created_target)
    with _target_lock(target):
        _ensure_folder(target / BLOBS)
        snapshots = target / SNAPSHOTS
        _ensure_folder(snapshots)
        store = BlobStore(target / BLOBS)
        try:
            cleaned = store.clean_temp() + store.seal()
        except BlobIntegrityError:
            msg = "the backup's blob folder holds a link or something else out of place"
            raise BackupError(msg) from None
        name = now().astimezone(UTC).strftime(STAMP_FORMAT)
        folder = snapshots / name
        try:
            folder.mkdir()
        except FileExistsError:
            msg = "a snapshot with this time is already in the target; it was not changed"
            raise BackupError(msg) from None
        created: list[Path] = []
        try:
            partial = folder / (DATABASE + PARTIAL)
            created.append(partial)
            rows = _snapshot(paths.db, partial)
            try:
                counts = _copy_blobs(rows, BlobStore(paths.blobs), store)
            except BlobIntegrityError:
                msg = "a folder of the backup's blob store is a link or not a folder"
                raise BackupError(msg) from None
            if config_file is not None:
                try:
                    settings = config_file.read_bytes()
                except OSError:
                    msg = "the settings file could not be read"
                    raise BackupError(msg) from None
                _write_new(settings, folder / CONFIG, created)
            _publish(partial, folder / DATABASE, created)
        except BaseException:
            _undo(folder, created)
            raise
        incomplete = _count_incomplete(snapshots, folder)
        if private:
            # The folder alone is not the backup: every folder and file below it, old and new.
            private = tree(target)
    return BackupResult(
        snapshot=name,
        copied=counts.copied,
        present=counts.present,
        missing=counts.missing,
        damaged=counts.damaged,
        unreadable=counts.unreadable,
        mismatched=counts.mismatched,
        incomplete=incomplete,
        cleaned=cleaned,
        private=private,
    )
