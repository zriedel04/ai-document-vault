"""The blob store: original files, content-addressed and read-only (ADR 0006).

An imported file is copied byte for byte to ``<root>/sha256/aa/bb/<hash>.<ext>``, where ``<hash>``
is the SHA-256 of its bytes and ``aa``, ``bb`` are its first two byte pairs. Nothing about the
file's human name, folder or origin is in the path: those live in the database, so a name can never
collide or climb out of the store, and the same bytes are stored once however often they are
imported.

A file becomes visible under its final name only once it is complete: it is copied to a temporary
file inside the store (same volume), flushed to disk and then linked to its final name, which is
then marked read-only, so a crash or a refused copy leaves at most a temporary file, never a partial
blob. (The file is flushed, but the folder entry is not forced to disk, so after a power cut the
newest import may be missing; the import is then simply repeated.) A crash between the link and the
read-only mark can leave a finished blob writable, so start-up runs ``clean_temp()`` and ``seal()``.

Imports of the same bytes are serialised by a claim on the hash (a lock file in the temporary
folder), whatever extension each import brings: the extension is part of the file name, so a rename
or a link alone cannot make two imports under ``.txt`` and ``.md`` collide.

Only real files and real folders are part of the store. A symbolic link, a Windows junction or any
other reparse point where a blob or one of its folders belongs is treated as damage: it is never
read as a blob, changed, published into or deleted through (a link could lead outside the store,
and the store must never alter a file it does not own), and ``scan()`` reports it. The check is
made on each level just before it is used, not held across the operation, so it guards a store
that was damaged or edited by hand, not one under attack by a process that is racing it. The vault
folder itself may be a link the owner set up. Nothing here logs file names or content.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

# Always use ``fullmatch`` with these (``$`` also matches before a trailing newline).
SHA256: Final = re.compile(r"[0-9a-f]{64}")
EXTENSION: Final = re.compile(r"[a-z0-9]{1,10}")
BLOB_NAME: Final = re.compile(r"([0-9a-f]{64})(?:\.([a-z0-9]{1,10}))?")
ALGORITHM: Final = "sha256"
TEMP_FOLDER: Final = ".tmp"
CHUNK_BYTES: Final = 1024 * 1024
READ_ONLY: Final = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
WRITABLE: Final = stat.S_IWUSR
LOCK_SUFFIX: Final = ".lock"
LOCK_WAIT_SECONDS: Final = 60.0
LOCK_POLL_SECONDS: Final = 0.01
UNLINK_TRIES: Final = 100
IS_WINDOWS: Final = os.name == "nt"


class BlobError(Exception):
    """Base class for blob store errors. Messages never contain a file name or content."""


class BlobTooLargeError(BlobError):
    """The file is larger than the limit the caller set, so nothing was stored."""


class BlobNotFoundError(BlobError):
    """No blob has this hash."""


class BlobIntegrityError(BlobError):
    """Something that is not a blob occupies the place a blob belongs, so it cannot be stored
    there. The store never deletes or replaces it: that is for the integrity check and the owner."""


class BlobBusyError(BlobError):
    """Another import of the same bytes did not finish in time (a lock left by a crash is removed
    by ``clean_temp()`` at start-up)."""


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size: int
    path: Path
    created: bool  # False if these exact bytes were already in the store


@dataclass(frozen=True)
class Scan:
    """What is in the store: the well-formed blobs, and the entries that are not (reported, not
    ignored, so an integrity check can say that something odd sits in the store)."""

    blobs: tuple[tuple[str, Path], ...]
    strays: tuple[Path, ...]


def check_sha256(value: str) -> str:
    if not SHA256.fullmatch(value):
        msg = "a SHA-256 hash is 64 lower-case hexadecimal characters"
        raise ValueError(msg)
    return value


def normalise_extension(extension: str) -> str:
    """The extension as stored: lower case, no dot, letters and digits only; may be empty."""
    cleaned = extension.removeprefix(".").lower()
    if cleaned and not EXTENSION.fullmatch(cleaned):
        msg = "an extension is 1 to 10 letters or digits"
        raise ValueError(msg)
    return cleaned


HEX_PAIR: Final = re.compile(r"[0-9a-f]{2}")


def _lstat(path: Path) -> os.stat_result | None:
    """The entry's own status, without following a link; None if it is not there."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_link(status: os.stat_result) -> bool:
    """A symbolic link, or on Windows a junction or other reparse point (``st_file_attributes`` is
    only there on Windows)."""
    reparse = getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return stat.S_ISLNK(status.st_mode) or bool(reparse)


def _is_real_dir(status: os.stat_result | None) -> bool:
    return status is not None and stat.S_ISDIR(status.st_mode) and not _is_link(status)


def _is_real_file(status: os.stat_result | None) -> bool:
    return status is not None and stat.S_ISREG(status.st_mode) and not _is_link(status)


def _blob_status(path: Path) -> os.stat_result | None:
    """The status of a regular file that only the store holds, or None for anything else: a link,
    a folder, or a file with other names (a hard link to a file outside the store has the same
    bytes and the same permissions, so changing its mode would change the outside file). Windows
    can keep counting a name for a moment after it was removed, so there a count above one is
    looked at again before it is believed."""
    for attempt in range(UNLINK_TRIES if IS_WINDOWS else 1):
        status = _lstat(path)
        if not _is_real_file(status):
            return None
        assert status is not None  # noqa: S101 - narrowed by the check above, for the type checker
        if status.st_nlink == 1:
            return status
        if attempt < UNLINK_TRIES - 1 and IS_WINDOWS:
            time.sleep(LOCK_POLL_SECONDS)
    return None


def _real_file_mode(path: Path) -> int:
    """The permission bits of a blob; anything else (a link, a folder, a file shared with another
    name) is not one and is never changed."""
    status = _blob_status(path)
    if status is None:
        msg = "something that is not a file held only by the store is where a blob belongs"
        raise BlobIntegrityError(msg)
    return status.st_mode


def _read_only(path: Path) -> None:
    _real_file_mode(path)
    path.chmod(READ_ONLY)


def _writable(path: Path) -> None:
    path.chmod(_real_file_mode(path) | WRITABLE)


class BlobStore:
    """The ``blobs/`` folder of a vault."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # --- Paths -----------------------------------------------------------------------------

    def folder_for(self, sha256: str) -> Path:
        check_sha256(sha256)
        return self.root / ALGORITHM / sha256[0:2] / sha256[2:4]

    def path_for(self, sha256: str, extension: str = "") -> Path:
        """Where the blob with this hash lives (or would live, with this extension)."""
        suffix = normalise_extension(extension)
        name = f"{sha256}.{suffix}" if suffix else sha256
        return self.folder_for(sha256) / name

    def _safe_folder(self, sha256: str, *, create: bool) -> Path | None:
        """The folder for this hash, after checking that each level under the vault folder
        (``sha256``, ``aa``, ``bb``) is a real folder and not a link. None if it is not there and
        ``create`` is False; with ``create`` the missing levels are made, one at a time, so that
        no level is ever made through another one. A link or a file in a level raises
        ``BlobIntegrityError``."""
        folder = self.folder_for(sha256)
        for level in (folder.parent.parent, folder.parent, folder):
            status = _lstat(level)
            if status is None:
                if not create:
                    return None
                level.mkdir(parents=level == folder.parent.parent, exist_ok=True)
                status = _lstat(level)
            if not _is_real_dir(status):
                msg = "something that is not a folder is where a blob folder belongs"
                raise BlobIntegrityError(msg)
        return folder

    def find(self, sha256: str) -> Path | None:
        """The stored file for this hash, whatever extension it was stored under, or None. A link
        is not a blob, whether it is the file or one of its folders."""
        try:
            folder = self._safe_folder(sha256, create=False)
        except BlobIntegrityError:
            return None
        if folder is None:
            return None
        for entry in folder.iterdir():
            match = BLOB_NAME.fullmatch(entry.name)
            if match and match.group(1) == sha256 and _blob_status(entry) is not None:
                return entry
        return None

    def exists(self, sha256: str) -> bool:
        return self.find(sha256) is not None

    # --- Writing ---------------------------------------------------------------------------

    def put_file(
        self, source: Path, extension: str | None = None, *, max_bytes: int | None = None
    ) -> StoredBlob:
        """Copy a file into the store. The extension defaults to the source's own."""
        suffix = source.suffix if extension is None else extension
        with source.open("rb") as stream:
            return self.put(stream, suffix, max_bytes=max_bytes)

    def put(
        self, stream: BinaryIO, extension: str = "", *, max_bytes: int | None = None
    ) -> StoredBlob:
        """Store the bytes of ``stream``; return where they are and whether they were new.

        If the same bytes are already stored (under any extension) nothing is written and the
        existing file is returned. ``max_bytes`` stops a copy that turns out to be too large
        before it fills the disk.
        """
        suffix = normalise_extension(extension)
        temp = self._temp_path()
        digest = hashlib.sha256()
        size = 0
        try:
            with temp.open("xb") as out:
                while chunk := stream.read(CHUNK_BYTES):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        msg = "the file is larger than the size limit"
                        raise BlobTooLargeError(msg)
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha256 = digest.hexdigest()
            with self._claim(sha256):
                return self._store(temp, sha256, size, suffix)
        finally:
            temp.unlink(missing_ok=True)

    def _store(self, temp: Path, sha256: str, size: int, suffix: str) -> StoredBlob:
        """Publish the finished copy, holding the claim on its hash."""
        existing = self.find(sha256)
        if existing is not None:
            self._seal_file(existing)  # a crash may have left it writable
            return StoredBlob(sha256, size, existing, created=False)
        final = self.path_for(sha256, suffix)
        self._safe_folder(sha256, create=True)
        if not self._publish(temp, final):
            # The name is taken. Only a real blob counts as the winner: a folder or another
            # odd entry in its place is not one, and pointing a caller at it would be wrong.
            winner = self.find(sha256)
            if winner is None:
                msg = "something that is not a blob occupies the place for this blob"
                raise BlobIntegrityError(msg)
            self._seal_file(winner)
            return StoredBlob(sha256, size, winner, created=False)
        # Drop the temporary name first: the read-only mark belongs to the file, so on Windows
        # a read-only file cannot be unlinked through any of its names.
        temp.unlink(missing_ok=True)
        _read_only(final)
        return StoredBlob(sha256, size, final, created=True)

    @staticmethod
    def _seal_file(path: Path) -> None:
        if _real_file_mode(path) & WRITABLE:
            _read_only(path)

    @contextmanager
    def _claim(self, sha256: str) -> Iterator[None]:
        """Hold the one claim on a hash, so imports (and a delete) of the same bytes take turns
        whatever their extensions. The claim is a file created only if it does not exist."""
        lock = self._temp_folder() / f"{check_sha256(sha256)}{LOCK_SUFFIX}"
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                pass
            except PermissionError:
                # Windows refuses to create a name whose deletion is still pending (the holder has
                # just released it), and says "access denied" instead of "exists". That is the
                # lock being busy, not a missing permission; elsewhere it is a real error.
                if not IS_WINDOWS:
                    raise
            if time.monotonic() > deadline:
                msg = "another import of the same file did not finish"
                raise BlobBusyError(msg) from None
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            self._release(lock)

    @staticmethod
    def _release(lock: Path) -> None:
        """Remove the lock. Windows can refuse for a moment while another program (a virus scanner)
        has the file open, so it tries again before giving up; a lock that stays would block the
        hash until the next start-up."""
        for attempt in range(UNLINK_TRIES):
            try:
                lock.unlink(missing_ok=True)
                return
            except PermissionError:
                if not IS_WINDOWS or attempt == UNLINK_TRIES - 1:
                    raise
                time.sleep(LOCK_POLL_SECONDS)

    @staticmethod
    def _publish(temp: Path, final: Path) -> bool:
        """Make the finished copy visible under its final name. False if it already exists.

        A hard link creates the name only if it is free, so when several imports of the same bytes
        race, exactly one of them wins on any system. (A plain rename replaces silently on Linux
        and macOS, and two racers would both report a new blob.) The temporary name is removed by
        the caller. A file system without hard links falls back to a rename, which still never
        leaves a partial blob.
        """
        try:
            os.link(temp, final)
        except FileExistsError:
            return False
        except OSError:
            if os.path.lexists(final):  # never rename over an entry, whatever it is
                return False
            try:
                temp.rename(final)
            except FileExistsError:
                return False
            except OSError:
                # Windows reports a folder in the way as an access error, not as "exists".
                if os.path.lexists(final):
                    return False
                raise
        return True

    def _temp_folder(self) -> Path:
        """The temporary folder, made if it is missing. Anything but a real folder (a link, a file)
        is refused: copies, locks and the clean-up all work inside it."""
        folder = self.root / TEMP_FOLDER
        if _lstat(folder) is None:
            folder.mkdir(parents=True, exist_ok=True)
        if not _is_real_dir(_lstat(folder)):
            msg = "something that is not a folder is where the temporary folder belongs"
            raise BlobIntegrityError(msg)
        return folder

    def _temp_path(self) -> Path:
        return self._temp_folder() / uuid.uuid4().hex

    def clean_temp(self) -> int:
        """Remove what a crashed copy left behind, locks included. Run at start-up, before
        ``seal()`` and never while copying. A link is removed as a link (its target is never
        touched), a folder is left alone, and a temporary folder that is itself a link is an
        error, so the clean-up cannot delete files outside the store."""
        folder = self.root / TEMP_FOLDER
        status = _lstat(folder)
        if status is None:
            return 0
        if not _is_real_dir(status):
            msg = "something that is not a folder is where the temporary folder belongs"
            raise BlobIntegrityError(msg)
        removed = 0
        for entry in folder.iterdir():
            entry_status = _lstat(entry)
            if entry_status is None:
                continue
            linked = _is_link(entry_status)
            is_folder = stat.S_ISDIR(entry_status.st_mode) or (linked and entry.is_dir())
            if _is_real_file(entry_status) or (linked and not is_folder):
                entry.unlink()
                removed += 1
        return removed

    def seal(self) -> int:
        """Make every blob read-only again and return how many were not. Run at start-up after
        ``clean_temp()`` (a crash between linking and unlinking leaves the temporary name as a
        second name for a finished blob, which ``scan`` does not count as a blob until the clean-up
        has removed it): a crash between publishing a blob and marking it read-only leaves a
        finished blob that anyone could edit in place under a name that still claims its hash.
        (An edit is found by ``verify``; sealing only closes the door.)"""
        sealed = 0
        for _, path in self.scan().blobs:
            if _real_file_mode(path) & WRITABLE:
                _read_only(path)
                sealed += 1
        return sealed

    # --- Reading and checking --------------------------------------------------------------

    def open(self, sha256: str) -> BinaryIO:
        path = self.find(sha256)
        if path is None:
            msg = "no blob has this hash"
            raise BlobNotFoundError(msg)
        return path.open("rb")

    def verify(self, sha256: str) -> bool:
        """Re-hash the stored bytes. False if they no longer match their name (damage or edit)."""
        with self.open(sha256) as stream:
            digest = hashlib.sha256()
            while chunk := stream.read(CHUNK_BYTES):
                digest.update(chunk)
        return digest.hexdigest() == sha256

    def scan(self) -> Scan:
        """List every blob, and every entry under ``sha256/`` that is not one.

        A blob is a regular file named ``<hash>`` or ``<hash>.<ext>`` in the folder its hash names.
        The only folders that are part of the layout are ``aa`` and ``aa/bb`` (two hex digits each).
        Anything else is reported and not entered: a file out of place, a folder where a blob
        belongs (even one named like a blob), a folder with another name, and any link. The
        temporary folder is not part of the store's content.
        """
        blobs: list[tuple[str, Path]] = []
        strays: list[Path] = []
        base = self.root / ALGORITHM
        status = _lstat(base)
        if status is None:
            return Scan((), ())
        if not _is_real_dir(status):
            return Scan((), (base,))
        for first in self._entries(base):
            if not (HEX_PAIR.fullmatch(first.name) and _is_real_dir(_lstat(first))):
                strays.append(first)
                continue
            for second in self._entries(first):
                if not (HEX_PAIR.fullmatch(second.name) and _is_real_dir(_lstat(second))):
                    strays.append(second)
                    continue
                for entry in self._entries(second):
                    match = BLOB_NAME.fullmatch(entry.name)
                    if (
                        match
                        and _blob_status(entry) is not None
                        and entry.parent == self.folder_for(match.group(1))
                    ):
                        blobs.append((match.group(1), entry))
                    else:
                        strays.append(entry)
        return Scan(tuple(blobs), tuple(strays))

    @staticmethod
    def _entries(folder: Path) -> list[Path]:
        """The entries of a real folder, in a fixed order."""
        return sorted(folder.iterdir())

    def iter_hashes(self) -> Iterator[str]:
        yield from (sha256 for sha256, _ in self.scan().blobs)

    # --- Deleting --------------------------------------------------------------------------

    def delete(self, sha256: str) -> bool:
        """Permanently remove a blob (the one place a read-only original is ever touched).

        Returns False if there was no such blob. Empty folders it leaves are removed too.
        """
        with self._claim(sha256):
            path = self.find(sha256)
            if path is None:
                return False
            _writable(path)
            try:
                path.unlink()
            except BaseException:
                # A blob that could not be removed must not stay writable under its hash.
                with suppress(OSError):
                    _read_only(path)
                raise
            for folder in (path.parent, path.parent.parent):
                try:
                    folder.rmdir()
                except OSError:
                    break  # not empty: other blobs share this prefix
            return True
