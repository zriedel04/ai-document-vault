"""Write ``manifest.json`` for a folder of the owner's own files (``make-manifest``).

The embedding spike reads a corpus through its manifest (the format of ``demo/corpus``). This
module lists a folder, subfolders included, so the owner does not type one by hand: each file of a
kind the spikes know, with its path, kind, size, SHA-256, page count for a PDF, whether it needs
OCR, and which earlier file it is a byte-for-byte copy of.

Only counts leave this module. A file name, a path or the folder itself is never printed: they
are the owner's own names (``Clients/Patient-Name/diagnosis.pdf`` is a possible one), so every
file that is not listed is counted under a fixed reason instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vault_spikes.corpus import PdfPages, SpikeInputError, load_manifest, safe_file
from vault_spikes.errors import OwnValueError

MANIFEST = "manifest.json"

# The kind of each suffix. Text files and DOCX are what the corpus reader reads directly
# (``corpus.TEXT_SUFFIXES`` and ``.docx``), a PDF is read with pypdfium2, and the images are those
# the OCR spike reads (``spike_ocr.IMAGE_SUFFIXES``); a test ties each list to this table.
# ``.pdf`` is not here: whether it is ``pdf_text`` or ``pdf_scan`` depends on its pages.
KIND_BY_SUFFIX = {
    ".txt": "txt",
    ".md": "md",
    ".csv": "csv",
    ".docx": "docx",
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".bmp": "bmp",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".webp": "webp",
}
PDF_SUFFIX = ".pdf"
IMAGE_KINDS = frozenset({"png", "jpg", "bmp", "tiff", "webp"})
KINDS = ("pdf_text", "pdf_scan", "docx", "txt", "md", "csv", *sorted(IMAGE_KINDS))
"""Every kind this module writes, in the order the summary prints them."""

DESCRIPTION = "Written by vault_spikes make-manifest."
# Office keeps "~$name.docx" next to an open document; it is a lock file, not a document.
_LOCK_PREFIX = "~$"
_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM


@dataclass
class Tally:
    """What happened to every entry below the folder. Each field is a count, never a name."""

    listed: int = 0
    duplicates: int = 0
    by_kind: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    unsupported: int = 0
    unreadable: int = 0
    links: int = 0
    hidden: int = 0
    folders_unreadable: int = 0
    names_unusable: int = 0

    def lines(self) -> list[str]:
        """The summary the command prints: fixed text and numbers."""
        lines = [f"documents listed: {self.listed}"]
        lines += [f"  {kind}: {self.by_kind[kind]}" for kind in KINDS]
        lines += [
            f"exact duplicates marked: {self.duplicates}",
            f"skipped, not a kind the spikes read: {self.unsupported}",
            f"skipped, could not be read: {self.unreadable}",
            f"skipped, links and junctions: {self.links}",
            f"skipped, hidden or system: {self.hidden}",
            f"skipped, folders that could not be listed: {self.folders_unreadable}",
            f"skipped, names the manifest cannot hold: {self.names_unusable}",
        ]
        return lines


def _is_link(entry: os.DirEntry[str], info: os.stat_result) -> bool:
    """A symbolic link, a junction or any other Windows reparse point (a junction has the reparse
    attribute, not the link mode; a OneDrive file that is only online is a reparse point too)."""
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return entry.is_symlink() or entry.is_junction() or bool(reparse)


def _is_hidden(name: str, info: os.stat_result) -> bool:
    """Hidden by name (a leading dot, an Office lock file) or by the Windows hidden or system
    attribute (``desktop.ini``, ``Thumbs.db`` and the like)."""
    hidden = getattr(info, "st_file_attributes", 0) & _HIDDEN
    return name.startswith((".", _LOCK_PREFIX)) or bool(hidden)


def _files(root: Path, tally: Tally) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Every regular file below ``root`` with its POSIX path relative to it, never entering a
    linked folder. The root's own manifest is left out without being counted."""
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        try:
            with os.scandir(folder) as listing:
                entries = list(listing)
        except OSError:
            if not prefix:
                msg = "the --corpus folder cannot be listed"
                raise OwnValueError(msg) from None
            tally.folders_unreadable += 1
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
                link = _is_link(entry, info)
            except OSError:
                tally.unreadable += 1
                continue
            if link:
                tally.links += 1
            elif _is_hidden(entry.name, info):
                tally.hidden += 1
            elif stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), f"{prefix}{entry.name}/"))
            elif not stat.S_ISREG(info.st_mode):
                tally.unsupported += 1  # a device, a socket, a pipe
            elif prefix or entry.name.casefold() != MANIFEST:
                yield f"{prefix}{entry.name}", Path(entry.path), info


def _usable_name(relative: str) -> bool:
    """Can the manifest hold this path so that the loader reads it back as the same file? JSON in
    UTF-8 cannot hold a name that is not valid Unicode (Linux allows any bytes, Windows an unpaired
    surrogate), and the loader reads a backslash as a folder separator, which a Linux name may
    contain. Control characters would make the file hard to read and edit by hand."""
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\\" not in relative and not any(ord(char) < 32 for char in relative)


def _digest(path: Path) -> tuple[str, int]:
    """SHA-256 and size of the bytes read, in one pass (the size is what was hashed)."""
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class _Pdf:
    kind: str
    pages: int


def _pdf(path: Path, pdf_pages: PdfPages) -> _Pdf | None:
    """``pdf_text`` if any page has text, ``pdf_scan`` (needs OCR) if none has; None if the PDF
    cannot be read (encrypted, corrupt), so it is skipped and counted."""
    pages = pdf_pages(path)
    if pages is None:
        return None
    return _Pdf("pdf_text" if any(page.strip() for page in pages) else "pdf_scan", len(pages))


def build_manifest(root: Path, pdf_pages: PdfPages) -> tuple[dict[str, Any], Tally]:
    """The manifest of every file of a known kind below ``root``, and the counts of what happened.

    Files are listed in sorted path order, and of a set of byte-identical files the first in that
    order is the original and every later one is marked ``exact_duplicate_of`` it. Paths that the
    loader would take for one file (they differ only in case, as on Windows) keep only the first.
    ``pdf_pages`` reads a PDF's page texts (``adapters.pdf_pages_for_corpus``); a library that is
    missing stops the run from inside it, before anything is written.
    """
    if not root.is_dir():
        msg = "the --corpus folder does not exist or is not a folder"
        raise OwnValueError(msg)
    tally = Tally()
    documents: list[dict[str, Any]] = []
    originals: dict[str, str] = {}
    pdfs: dict[str, _Pdf] = {}
    identities: set[str] = set()
    for relative, path, _ in sorted(_files(root, tally), key=lambda found: found[0]):
        suffix = Path(relative).suffix.lower()
        kind = KIND_BY_SUFFIX.get(suffix)
        if kind is None and suffix != PDF_SUFFIX:
            tally.unsupported += 1
            continue
        identity = relative.casefold()
        if not _usable_name(relative) or identity in identities:
            tally.names_unusable += 1
            continue
        try:
            sha256, size = _digest(path)
        except OSError:
            tally.unreadable += 1
            continue
        pages: int | None = None
        if kind is None:  # a PDF; a copy is the same file, so it is read once
            pdf = pdfs.get(sha256) or _pdf(path, pdf_pages)
            if pdf is None:
                tally.unreadable += 1
                continue
            pdfs[sha256] = pdf
            kind, pages = pdf.kind, pdf.pages
        identities.add(identity)
        row: dict[str, Any] = {
            "path": relative,
            "kind": kind,
            "bytes": size,
            "sha256": sha256,
            "pages": pages,
            "needs_ocr": kind == "pdf_scan" or kind in IMAGE_KINDS,
        }
        if sha256 in originals:
            row["exact_duplicate_of"] = originals[sha256]
            tally.duplicates += 1
        else:
            originals[sha256] = relative
        documents.append(row)
        tally.listed += 1
        tally.by_kind[kind] += 1
    return {"description": DESCRIPTION, "documents": documents}, tally


def check_target(root: Path, *, force: bool) -> None:
    """Refuse before the folder is read if ``manifest.json`` is there and ``--force`` was not
    given, or if it is anything but a regular file (a folder, a link), which is never replaced."""
    try:
        info = (root / MANIFEST).lstat()
    except FileNotFoundError:
        return
    except OSError:
        msg = "the manifest in the --corpus folder cannot be checked"
        raise OwnValueError(msg) from None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & (
        stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        msg = "manifest.json in the --corpus folder is not a regular file; it is not replaced"
        raise OwnValueError(msg)
    if not force:
        msg = "the --corpus folder already has a manifest.json; pass --force to replace it"
        raise OwnValueError(msg)


def write_manifest(root: Path, manifest: dict[str, Any], *, force: bool) -> None:
    """Write ``manifest.json`` atomically: into a temporary file in the same folder, read back
    with the spike's own loader (every path must be a file inside the folder), then put in place.

    Without ``force`` the file is published by :func:`_publish_new`, which never replaces a
    manifest that appeared after :func:`check_target`. The temporary file starts with a dot, so a
    run that is killed leaves a hidden file that the next run does not list.
    """
    check_target(root, force=force)
    target = root / MANIFEST
    handle, temporary = tempfile.mkstemp(dir=root, prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        documents = load_manifest(Path(temporary))
        if len(documents) != len(manifest["documents"]):
            msg = "the manifest did not read back as written"
            raise SpikeInputError(msg)
        for document in documents:
            safe_file(root, document.path)
        if force:
            os.replace(temporary, target)
            return
        _publish_new(Path(temporary), target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


_APPEARED = "a manifest.json appeared in the --corpus folder during the run; not replaced"


def _publish_new(temporary: Path, target: Path) -> None:
    """Put ``temporary`` at ``target`` only if nothing is there, with an operation that checks and
    creates in one step, so there is no window between a check and a replace in which a manifest
    made by another process could be overwritten.

    A hard link is tried first: it shows the whole file at once. A file system without hard links
    (FAT, exFAT) refuses it, and then Windows renames, which never replaces an existing name, and
    any other system creates the name exclusively (``O_CREAT | O_EXCL``, which also refuses a link
    in its place) and copies the bytes into it. ``os.replace`` is never used here."""
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise OwnValueError(_APPEARED) from None
    except OSError:
        pass
    else:
        return
    if os.name == "nt":
        try:
            os.rename(temporary, target)  # MoveFileExW without MOVEFILE_REPLACE_EXISTING
        except FileExistsError:
            raise OwnValueError(_APPEARED) from None
        return
    _copy_exclusive(temporary, target)


def _copy_exclusive(temporary: Path, target: Path) -> None:
    """Create ``target`` exclusively and copy ``temporary`` into it. If the copy fails, the file
    this call created is removed, but only while the name still holds that same file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(target, flags, 0o666)
    except FileExistsError:
        raise OwnValueError(_APPEARED) from None
    created = os.fstat(handle)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(temporary.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            now = target.lstat()
            if (now.st_dev, now.st_ino) == (created.st_dev, created.st_ino):
                target.unlink()
        raise
