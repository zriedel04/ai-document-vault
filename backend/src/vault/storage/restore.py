"""Restore a backup snapshot into a new vault folder (``docs/architecture.md``, section 17).

The source is one snapshot of a backup folder (:mod:`vault.storage.backup`), named by its folder
(``<backup>/snapshots/<time>``), or the backup folder itself, which means its latest complete
snapshot (the newest folder with a ``vault.db``). The target is a vault folder that does not exist
yet or is empty: a restore never writes over a vault, or into any folder that holds something.

Everything is written into a new folder beside the target, named ``.vault-restore-<random>``, and
that folder is renamed to the target's name only once all of it is there. So a restore that fails,
is interrupted or is killed never leaves anything under the target's name that could look like a
vault (a vault folder with blobs and no ``vault.db`` would start as an empty vault and move every
blob aside). A failed or interrupted run removes its own folder; a killed one leaves it, and the
next restore into the same parent folder counts such folders so the owner can delete them.

Into that folder go, in this order:

- ``vault.db``: a byte copy of the snapshot's database, flushed, then checked with SQLite's full
  ``PRAGMA integrity_check``. A database that fails it stops the restore. Its ``blobs`` rows are
  the list of original files to restore.
- ``blobs/``: each listed blob, copied with the blob store's own ``put`` from the backup's store,
  hashed as it is written and published only if the hash and the exact size are the ones its row
  gives, then made read-only (the backup's own copy step, ``backup._copy_blobs``). A blob the
  backup does not hold, or whose bytes do not match, is counted and not restored; the rest of the
  vault is still restored, because one lost file must not make every other one unrecoverable. The
  counts say so and the command fails, and ``vault.ps1 check`` on the new vault names the hashes.
- ``local-token``: the snapshot's token (ADR 0016: "backed up with the vault"), so the browser's
  saved token keeps working; owner-only, like the one the server makes. A snapshot without one, or
  with one that start-up would refuse (``security.parse_token``), restores without it, and the
  server makes a new one at its first start.
- ``config.toml``: the snapshot's settings file, where section 5 of the plan places it. The app
  reads its settings from ``backend\\config.toml`` (or ``AIVAULT_CONFIG``), not from the vault
  folder, so this copy is for the owner to compare: its ``vault.path`` still names the folder the
  backup was taken from, and the restore changes neither file.

Derived data is not written. Chunks and the keyword index are in ``vault.db`` (section 17 keeps
them in the snapshot) and come back with it; ``index.db`` and ``derived/`` have no writer yet, so
there is nothing to queue. The first start of the app on the new folder creates its other folders,
migrates an older database, queues again any job the snapshot caught running and seals the store.

The same checks as the backup's apply (``backup.check_place``): no link or junction on the way to
the source or the target, and no link anywhere a file is read (the blob store's own checks); a
target inside the backup folder, inside a vault folder that already holds files, or inside a Git
working tree is refused. On Windows the new folder is made private to its owner before anything is
written into it, and every file and folder below it is checked after (``acl.verify_tree``): the
app refuses to start on a vault that other accounts can read, so a restore that cannot be private
is refused too. Messages are fixed text: no file name, hash or path appears in them.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Final, Literal

from vault.core import security
from vault.storage import acl
from vault.storage.backup import (
    BLOBS,
    CONFIG,
    DATABASE,
    OWNER_ONLY_WRITABLE,
    SNAPSHOTS,
    STAMP,
    TOKEN,
    TOKEN_MAX_BYTES,
    BackupError,
    _copy_blobs,
    check_place,
    checked_blob_rows,
    open_real_file,
)
from vault.storage.blobs import (
    WRITABLE,
    BlobIntegrityError,
    BlobStore,
    _is_real_dir,
    _is_real_file,
    _lstat,
)

WORK_PREFIX: Final = ".vault-restore-"
WORK_NAME: Final = re.compile(r"\.vault-restore-[0-9a-f]{32}")  # always ``fullmatch``
CONFIG_MAX_BYTES: Final = 1024 * 1024  # a settings file is a few hundred bytes
FILE_MODES: Final = {
    DATABASE: OWNER_ONLY_WRITABLE,  # the app writes it
    CONFIG: OWNER_ONLY_WRITABLE,  # the owner may edit it
    TOKEN: security.TOKEN_FILE_MODE,  # as the server makes it
}
RENAME_TRIES: Final = 50  # Windows: a virus scanner may hold a new file for a moment
RENAME_WAIT_SECONDS: Final = 0.1
IS_WINDOWS: Final = sys.platform == "win32"

TokenState = Literal["restored", "absent", "invalid"]


class RestoreError(BackupError):
    """The restore cannot go on. The message is fixed text and names no file or folder."""


@dataclass(frozen=True)
class RestoreResult:
    snapshot: str  # the snapshot folder's name, a time (checked with ``STAMP``)
    restored: int  # blobs copied and verified
    missing: int  # blobs the snapshot lists that the backup does not hold
    damaged: int  # blobs whose bytes or row do not match their hash, not restored
    unreadable: int  # blobs that could not be read (another program may hold them)
    token_state: TokenState  # what became of the snapshot's local-token
    config: bool  # the snapshot had a settings file, now config.toml in the new vault
    leftovers: int  # folders of earlier restores that did not finish, beside the target

    @property
    def problems(self) -> int:
        return self.missing + self.damaged + self.unreadable


def _same_name(name: str, expected: str) -> bool:
    return os.path.normcase(name) == os.path.normcase(expected)


def find_snapshot(source: Path) -> Path:
    """The snapshot folder ``source`` names: a snapshot itself (``.../snapshots/<time>``), or a
    backup folder, meaning its latest complete snapshot. ``RestoreError`` if it is neither, if the
    snapshot did not finish (no ``vault.db``) or if its backup has no blob store."""
    source = check_place(source, noun="backup folder", git=False)
    if not _is_real_dir(_lstat(source)):
        msg = "the backup folder or snapshot to restore from was not found"
        raise RestoreError(msg)
    if STAMP.fullmatch(source.name) and _same_name(source.parent.name, SNAPSHOTS):
        snapshot = source
    else:
        snapshots = source / SNAPSHOTS
        if not _is_real_dir(_lstat(snapshots)):
            msg = (
                "the folder to restore from is neither a backup folder nor one of its snapshots "
                "(snapshots\\<time>)"
            )
            raise RestoreError(msg)
        complete = sorted(
            entry
            for entry in snapshots.iterdir()
            if STAMP.fullmatch(entry.name)
            and _is_real_dir(_lstat(entry))
            and _is_real_file(_lstat(entry / DATABASE))
        )
        if not complete:
            msg = "the backup folder holds no complete snapshot"
            raise RestoreError(msg)
        snapshot = complete[-1]  # the names are UTC times, so the last in order is the newest
    database = _lstat(snapshot / DATABASE)
    if database is None:
        msg = "this snapshot did not finish (it has no vault.db); choose another one"
        raise RestoreError(msg)
    if not _is_real_file(database):
        msg = "something that is not a file (or a link) is where the snapshot's database belongs"
        raise RestoreError(msg)
    if not _is_real_dir(_lstat(snapshot.parent.parent / BLOBS)):
        msg = "the backup folder has no blobs folder (or it is a link), so nothing can be restored"
        raise RestoreError(msg)
    return snapshot


def _holds_anything(folder: Path) -> bool:
    try:
        return any(folder.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return False


def check_restore_target(target: Path, backup_root: Path, vault_root: Path | None) -> Path:
    """The target as an absolute path, or ``BackupError``. It must not exist yet or be an empty
    real folder, outside the backup folder and outside every Git working tree. The configured
    vault folder is kept apart too when it holds files; when it is missing or empty (a drive that
    was replaced), the target may be that very folder, so its settings need no change."""
    apart = [(backup_root, "backup folder")]
    if vault_root is not None and _holds_anything(vault_root):
        apart.append((vault_root, "vault folder"))
    target = check_place(target, noun="restore target", apart=apart)
    if _holds_anything(target):
        msg = (
            "the restore target already holds files; restore into a new or empty folder "
            "(a restore never writes over a vault)"
        )
        raise RestoreError(msg)
    return target


def _copy_stream(source: BinaryIO, final: Path, mode: int) -> None:
    """Write the stream to a new file (never over one), flush it and give it exactly ``mode``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(final, flags, OWNER_ONLY_WRITABLE), "wb") as out:
        shutil.copyfileobj(source, out)
        out.flush()
        os.fsync(out.fileno())
    final.chmod(mode)


def _read_small(path: Path, noun: str, limit: int) -> bytes | None:
    """The file's bytes, at most ``limit`` and one more (so a caller can tell a file that is too
    large), or None if there is none."""
    handle = open_real_file(path, noun)
    if handle is None:
        return None
    with handle:
        return handle.read(limit + 1)


def _write(data: bytes, final: Path) -> None:
    with io.BytesIO(data) as stream:
        _copy_stream(stream, final, FILE_MODES[final.name])


def _make_private(folder: Path) -> None:
    """On Windows, make the new folder private to its owner before anything is written into it,
    as start-up does for a new vault (``layout.ensure_vault``)."""
    if not acl.IS_WINDOWS:
        return
    try:
        acl.secure_vault_folder(folder, created=True)
    except acl.VaultAccessError:
        # The library's message names the folder.
        msg = (
            "the new vault folder could not be made private to your account (a drive that keeps "
            "no permissions cannot hold a vault); nothing was restored"
        )
        raise RestoreError(msg) from None


def _verify_private(folder: Path) -> None:
    """On Windows, check every file and folder below the restored vault, as start-up does: the app
    would refuse to start on a vault with an entry other accounts can use."""
    if not acl.IS_WINDOWS:
        return
    try:
        acl.verify_tree(folder)
    except acl.VaultAccessError:
        msg = (
            "files of the restored vault could be used by other accounts on this PC, so the app "
            "would refuse to start with it; nothing was restored"
        )
        raise RestoreError(msg) from None


def _remove_tree(folder: Path) -> None:
    """Remove a folder this run made and everything in it, as far as it can. A link found inside
    is removed as a link and never followed, so nothing outside the folder is touched; a file
    with another name elsewhere (a hard link) is unlinked without changing its mode, which is the
    other name's too. Whatever cannot be removed stays, and the next restore counts the folder."""
    status = _lstat(folder)
    if status is None or not _is_real_dir(status):
        return
    with suppress(OSError):
        for entry in list(folder.iterdir()):
            entry_status = _lstat(entry)
            if entry_status is None:  # pragma: no cover - removed meanwhile
                continue
            if _is_real_dir(entry_status):
                _remove_tree(entry)
                continue
            with suppress(OSError):
                if _is_real_file(entry_status) and entry_status.st_nlink == 1:
                    entry.chmod(stat.S_IMODE(entry_status.st_mode) | WRITABLE)
                entry.unlink()  # a link is removed as a link
    with suppress(OSError):
        folder.rmdir()


def _count_leftovers(parent: Path, mine: Path) -> int:
    """Folders of earlier restores beside the target that never got their name (a killed run, or
    one still running). Counting is advice only, so a folder that cannot be listed counts none."""
    try:
        return sum(
            1
            for entry in parent.iterdir()
            if entry != mine and WORK_NAME.fullmatch(entry.name) and _is_real_dir(_lstat(entry))
        )
    except OSError:  # pragma: no cover - a parent that cannot be listed after the restore
        return 0


def _sync_folders(folder: Path) -> None:
    """Flush the entries of every folder below ``folder``, deepest first, so the rename that
    publishes the vault cannot reach the disk before the names it holds (a power cut would leave a
    vault that looks complete and lacks files). Windows cannot open a folder for this; NTFS keeps
    its own journal of names, written in order."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return
    for entry in folder.iterdir():
        if _is_real_dir(_lstat(entry)):
            _sync_folders(entry)
    _sync_folder(folder)


def _sync_folder(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _move_into_place(work: Path, target: Path) -> None:
    """Give the finished folder the target's name. An empty target folder is removed first (a
    rename onto a folder fails on Windows); anything that appeared in the target meanwhile stops
    the move, and nothing is replaced."""
    status = _lstat(target)
    emptied = False
    if status is not None:
        if not _is_real_dir(status) or _holds_anything(target):
            msg = (
                "something was put in the restore target while the restore ran; the restored "
                "vault was not moved there"
            )
            raise RestoreError(msg)
        target.rmdir()
        emptied = True
    for attempt in range(RENAME_TRIES if IS_WINDOWS else 1):
        try:
            work.rename(target)
            if not IS_WINDOWS:
                with suppress(OSError):  # the vault is in place; the flush is a precaution
                    _sync_folder(target.parent)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == RENAME_TRIES - 1:
                break
            time.sleep(RENAME_WAIT_SECONDS)  # pragma: no cover - Windows only
        except OSError:
            break
    if emptied:
        with suppress(OSError):
            target.mkdir()  # the empty folder the owner named, as it was
    msg = (
        "the restored vault could not be given the target's name (something holds or took the "
        "target); nothing was restored, try again"
    )
    raise RestoreError(msg)


def _restore_into(work: Path, snapshot: Path) -> RestoreResult:
    _make_private(work)
    database = open_real_file(snapshot / DATABASE, "the snapshot's database")
    if database is None:  # pragma: no cover - removed since find_snapshot looked
        msg = "this snapshot did not finish (it has no vault.db); choose another one"
        raise RestoreError(msg)
    with database:
        _copy_stream(database, work / DATABASE, FILE_MODES[DATABASE])
    try:
        rows = checked_blob_rows(work / DATABASE, full=True)
    except sqlite3.Error:
        rows = None
    if rows is None:
        msg = "the snapshot's database did not pass SQLite's integrity check; nothing was restored"
        raise RestoreError(msg)
    try:
        counts = _copy_blobs(
            rows, BlobStore(snapshot.parent.parent / BLOBS), BlobStore(work / BLOBS)
        )
    except BlobIntegrityError:
        msg = "a folder of a blob store is a link or not a folder; nothing was restored"
        raise RestoreError(msg) from None
    token = _read_small(snapshot / TOKEN, "the snapshot's token file", TOKEN_MAX_BYTES)
    state: TokenState = "absent"
    if token is not None:
        # Start-up's own rule: a token it would refuse is left out, and the server makes a new one.
        state = "invalid"
        if len(token) <= TOKEN_MAX_BYTES and security.parse_token(token) is not None:
            _write(token, work / TOKEN)
            state = "restored"
    config = _read_small(snapshot / CONFIG, "the snapshot's settings file", CONFIG_MAX_BYTES)
    if config is not None:
        if len(config) > CONFIG_MAX_BYTES:
            msg = (
                "the snapshot's settings file is far larger than a settings file; "
                "nothing was restored"
            )
            raise RestoreError(msg)
        _write(config, work / CONFIG)
    _verify_private(work)
    return RestoreResult(
        snapshot=snapshot.name,
        restored=counts.copied + counts.present,
        missing=counts.missing,
        damaged=counts.damaged + counts.mismatched,
        unreadable=counts.unreadable,
        token_state=state,
        config=config is not None,
        leftovers=0,
    )


def run_restore(source: Path, target: Path, *, vault_root: Path | None = None) -> RestoreResult:
    """Restore the snapshot ``source`` names into the new vault folder ``target`` (see the module
    docstring). ``vault_root`` is the vault the settings name, if any, kept apart from the target
    while it holds files."""
    snapshot = find_snapshot(source)
    target = check_restore_target(target, snapshot.parent.parent, vault_root)
    parent = target.parent
    if _lstat(parent) is None:
        parent.mkdir(parents=True, exist_ok=True)
    if not _is_real_dir(_lstat(parent)):  # pragma: no cover - check_place refused it already
        msg = "the folder that should hold the restore target is not a folder (or is a link)"
        raise RestoreError(msg)
    work = parent / f"{WORK_PREFIX}{uuid.uuid4().hex}"
    work.mkdir()
    try:
        result = _restore_into(work, snapshot)
        _sync_folders(work)
        _move_into_place(work, target)
    except BaseException:
        _remove_tree(work)
        raise
    return replace(result, leftovers=_count_leftovers(parent, work))
