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
newest import may be missing; the import is then simply repeated.) Nothing here logs file names or
content.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections.abc import Iterator
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


class BlobError(Exception):
    """Base class for blob store errors. Messages never contain a file name or content."""


class BlobTooLargeError(BlobError):
    """The file is larger than the limit the caller set, so nothing was stored."""


class BlobNotFoundError(BlobError):
    """No blob has this hash."""


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


def _read_only(path: Path) -> None:
    path.chmod(READ_ONLY)


def _writable(path: Path) -> None:
    path.chmod(path.stat().st_mode | WRITABLE)


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

    def find(self, sha256: str) -> Path | None:
        """The stored file for this hash, whatever extension it was stored under, or None."""
        folder = self.folder_for(sha256)
        if not folder.is_dir():
            return None
        for entry in folder.iterdir():
            match = BLOB_NAME.fullmatch(entry.name)
            if match and match.group(1) == sha256 and entry.is_file():
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
            existing = self.find(sha256)
            if existing is not None:
                return StoredBlob(sha256, size, existing, created=False)
            final = self.path_for(sha256, suffix)
            final.parent.mkdir(parents=True, exist_ok=True)
            if not self._publish(temp, final):
                return StoredBlob(sha256, size, final, created=False)
            # Drop the temporary name first: the read-only mark belongs to the file, so on Windows
            # a read-only file cannot be unlinked through any of its names.
            temp.unlink(missing_ok=True)
            _read_only(final)
            return StoredBlob(sha256, size, final, created=True)
        finally:
            temp.unlink(missing_ok=True)

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
            try:
                temp.rename(final)
            except FileExistsError:
                return False
        return True

    def _temp_path(self) -> Path:
        folder = self.root / TEMP_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        return folder / uuid.uuid4().hex

    def clean_temp(self) -> int:
        """Remove what a crashed copy left behind. Run at start-up, never while copying."""
        folder = self.root / TEMP_FOLDER
        if not folder.is_dir():
            return 0
        removed = 0
        for entry in folder.iterdir():
            if entry.is_file():
                entry.unlink()
                removed += 1
        return removed

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

        A blob is a file named ``<hash>`` or ``<hash>.<ext>`` in the folder its hash names. The
        temporary folder is not part of the store's content.
        """
        blobs: list[tuple[str, Path]] = []
        strays: list[Path] = []
        base = self.root / ALGORITHM
        if not base.is_dir():
            return Scan((), ())
        for entry in sorted(base.rglob("*")):
            if entry.is_dir():
                continue
            match = BLOB_NAME.fullmatch(entry.name)
            if match and entry.parent == self.folder_for(match.group(1)):
                blobs.append((match.group(1), entry))
            else:
                strays.append(entry)
        return Scan(tuple(blobs), tuple(strays))

    def iter_hashes(self) -> Iterator[str]:
        yield from (sha256 for sha256, _ in self.scan().blobs)

    # --- Deleting --------------------------------------------------------------------------

    def delete(self, sha256: str) -> bool:
        """Permanently remove a blob (the one place a read-only original is ever touched).

        Returns False if there was no such blob. Empty folders it leaves are removed too.
        """
        path = self.find(sha256)
        if path is None:
            return False
        _writable(path)
        path.unlink()
        for folder in (path.parent, path.parent.parent):
            try:
                folder.rmdir()
            except OSError:
                break  # not empty: other blobs share this prefix
        return True
