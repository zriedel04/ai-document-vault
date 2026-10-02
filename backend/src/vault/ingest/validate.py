"""Check a file before it is imported: size, type by content, extension, archive limits.

Imported files are untrusted input. The type is decided from the file's bytes, never from its name,
and the name's extension must agree with it, so a program renamed ``report.pdf`` or a PNG renamed
``notes.docx`` is refused before anything parses it. Only the formats the first release reads are
allowed (``docs/architecture.md``, section 8); PPTX, XLSX and CSV arrive with Phase 7.

A DOCX is a zip file, so its structure is checked without extracting anything: the entry count, the
total uncompressed size, the compression ratio, encrypted entries and member names that climb out
of the archive. The sizes come from the archive's own directory, which a hostile file can
understate, so the extractor must still bound what it reads. The directory itself is bounded
first: ``zipfile`` reads the whole central directory and builds an object for every record before
anything can be counted, so the end-of-directory record is read on its own (a few kilobytes from
the end of the file) and a file that claims too many entries or too large a directory is refused
before ``zipfile`` is asked to open it.

Text with a byte order mark is decoded with the encoding the mark names, and refused if it is not
valid in it or decodes to control characters, because a mark alone says nothing about the bytes
after it.

The verdict names a reason and a detected kind, never the file's name or any of its content.
Only the first ``HEAD_BYTES`` of a file are read to recognise it; a text file that turns binary
later is caught when it is extracted, not here.
"""

from __future__ import annotations

import codecs
import re
import struct
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

HEAD_BYTES: Final = 8192
# A PDF starts with "%PDF-1.x" or "%PDF-2.0", after at most some white space (the specification
# allows more junk before it; a file like that is refused rather than letting any text file that
# mentions "%PDF-" in its first kilobyte count as a PDF).
PDF_HEADER: Final = re.compile(rb"[ \t\r\n\x00]{0,16}%PDF-[12]\.[0-9]")
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE: Final = b"\xff\xd8\xff"
ZIP_SIGNATURE: Final = b"PK\x03\x04"
OLE_SIGNATURE: Final = bytes.fromhex("d0cf11e0a1b11ae1")  # legacy and password-protected Office
# Byte order marks and the encoding each names (UTF-32 LE starts like UTF-16 LE, so it comes first).
TEXT_BOMS: Final = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
# Bytes that do not occur in text: control characters other than tab, line breaks and form feed.
BINARY_BYTES: Final = frozenset(range(32)) - {9, 10, 12, 13} | {127}
MAX_BINARY_SHARE: Final = 0.05
DOCX_PARTS: Final = ("[Content_Types].xml", "word/document.xml")
ZIP_ENCRYPTED_FLAG: Final = 0x1
# The zip end-of-central-directory record, and the ZIP64 records before it (see ``zipfile``).
ZIP_END_SIGNATURE: Final = b"PK\x05\x06"
ZIP64_END_SIGNATURE: Final = b"PK\x06\x06"
ZIP64_LOCATOR_SIGNATURE: Final = b"PK\x06\x07"
ZIP_END_SIZE: Final = 22
ZIP64_END_SIZE: Final = 56
ZIP64_LOCATOR_SIZE: Final = 20
ZIP_MAX_COMMENT: Final = 1 << 16


class Kind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PNG = "png"
    JPG = "jpg"
    TEXT = "text"
    # Seen in content only, never accepted:
    ZIP = "zip"
    OLE = "ole"
    BINARY = "binary"


class Reason(StrEnum):
    NOT_A_FILE = "not_a_file"
    UNREADABLE = "unreadable"  # the file exists but the system will not let it be read
    EMPTY = "empty"
    TOO_LARGE = "too_large"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    CONTENT_MISMATCH = "content_mismatch"  # the bytes are not what the extension says
    LEGACY_OR_ENCRYPTED_OFFICE = "legacy_or_encrypted_office"
    UNREADABLE_ARCHIVE = "unreadable_archive"
    ENCRYPTED_ARCHIVE = "encrypted_archive"
    UNSAFE_ARCHIVE = "unsafe_archive"  # too many entries, too large, too compressed, odd names


# Extension (lower case, no dot) to the kind of content it must hold.
EXTENSIONS: Final[dict[str, Kind]] = {
    "pdf": Kind.PDF,
    "docx": Kind.DOCX,
    "png": Kind.PNG,
    "jpg": Kind.JPG,
    "jpeg": Kind.JPG,
    "txt": Kind.TEXT,
    "md": Kind.TEXT,
}
MIME_TYPES: Final = {
    ("pdf", "pdf"): "application/pdf",
    ("docx", "docx"): "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ("png", "png"): "image/png",
    ("jpg", "jpg"): "image/jpeg",
    ("txt", "text"): "text/plain",
    ("md", "text"): "text/markdown",
}


@dataclass(frozen=True)
class Limits:
    """The caps. The defaults are a first choice to be tuned on real documents."""

    max_bytes: int = 200 * 1024 * 1024
    max_zip_entries: int = 2_000
    max_zip_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_zip_ratio: int = 200  # uncompressed bytes per byte of the file
    # The central directory (a record of at least 46 bytes per entry) is read whole by zipfile.
    max_zip_directory_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class Verdict:
    """The outcome. ``extension`` and ``mime`` are set when the file is accepted: the canonical
    extension to store it under (``jpeg`` becomes ``jpg``) and its media type."""

    reason: Reason | None
    detected: Kind | None = None
    extension: str | None = None
    mime: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def _refuse(reason: Reason, detected: Kind | None = None) -> Verdict:
    return Verdict(reason, detected)


def sniff(head: bytes) -> Kind:
    """What the first bytes of a file say it is."""
    if PDF_HEADER.match(head):
        return Kind.PDF
    if head.startswith(PNG_SIGNATURE):
        return Kind.PNG
    if head.startswith(JPEG_SIGNATURE):
        return Kind.JPG
    if head.startswith(ZIP_SIGNATURE):
        return Kind.ZIP
    if head.startswith(OLE_SIGNATURE):
        return Kind.OLE
    for bom, encoding in TEXT_BOMS:
        if head.startswith(bom):
            return Kind.TEXT if _decodes_as_text(head[len(bom) :], encoding) else Kind.BINARY
    if not head:
        return Kind.TEXT
    if b"\x00" in head:  # UTF-16 and UTF-32 text carry a byte order mark and was handled above
        return Kind.BINARY
    if sum(1 for byte in head if byte in BINARY_BYTES) / len(head) > MAX_BINARY_SHARE:
        return Kind.BINARY
    return Kind.TEXT


def _decodes_as_text(data: bytes, encoding: str) -> bool:
    """Is ``data`` valid in ``encoding`` and free of NUL and a lot of control characters?

    The head may end in the middle of a character, which is not an error. This is the same test as
    for text without a byte order mark, made on characters instead of bytes.
    """
    try:
        text = codecs.getincrementaldecoder(encoding)("strict").decode(data, final=False)
    except UnicodeDecodeError:
        return False
    if "\x00" in text:
        return False
    controls = sum(1 for char in text if ord(char) in BINARY_BYTES)
    return controls <= MAX_BINARY_SHARE * len(text)


def _directory_size_ok(path: Path, size: int, limits: Limits) -> Reason | None:
    """Read only the end-of-directory record and refuse a zip that claims a directory too large to
    open. None if the claim is within the limits.

    This finds the record the way ``zipfile`` does (the last 22 bytes if they are a record with no
    comment, else the last signature in the final 64 KiB), so it judges the record ``zipfile`` would
    use, and follows the ZIP64 records the same way. ``zipfile`` reads ``size_cd`` bytes and builds
    an object for every record in them, so ``size_cd`` is what is bounded.
    """
    with path.open("rb") as stream:
        if size < ZIP_END_SIZE:
            return Reason.UNREADABLE_ARCHIVE
        stream.seek(size - ZIP_END_SIZE)
        record = stream.read(ZIP_END_SIZE)
        start = size - ZIP_END_SIZE
        if not (record[:4] == ZIP_END_SIGNATURE and record[-2:] == b"\x00\x00"):
            tail_start = max(size - ZIP_MAX_COMMENT - ZIP_END_SIZE, 0)
            stream.seek(tail_start)
            tail = stream.read()
            found = tail.rfind(ZIP_END_SIGNATURE)
            if found < 0:
                return Reason.UNREADABLE_ARCHIVE
            start = tail_start + found
            record = tail[found : found + ZIP_END_SIZE]
            if len(record) != ZIP_END_SIZE:
                return Reason.UNREADABLE_ARCHIVE
        _, _, _, _, entries, directory_bytes, _, _ = struct.unpack("<4s4H2LH", record)
        locator_start = start - ZIP64_LOCATOR_SIZE
        if locator_start >= ZIP64_END_SIZE:
            stream.seek(locator_start)
            locator = stream.read(ZIP64_LOCATOR_SIZE)
            if locator[:4] == ZIP64_LOCATOR_SIGNATURE:
                stream.seek(locator_start - ZIP64_END_SIZE)
                wide = stream.read(ZIP64_END_SIZE)
                if wide[:4] == ZIP64_END_SIGNATURE:
                    fields = struct.unpack("<4sQ2H2L4Q", wide)
                    entries, directory_bytes = fields[8], fields[9]
    if entries > limits.max_zip_entries or directory_bytes > limits.max_zip_directory_bytes:
        return Reason.UNSAFE_ARCHIVE
    return None


def _check_docx(path: Path, size: int, limits: Limits) -> Reason | None:
    """None if the zip is a well-formed DOCX within the limits, else why not."""
    try:
        reason = _directory_size_ok(path, size, limits)
        if reason is not None:
            return reason
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_zip_entries:
                return Reason.UNSAFE_ARCHIVE
            if any(entry.flag_bits & ZIP_ENCRYPTED_FLAG for entry in entries):
                return Reason.ENCRYPTED_ARCHIVE
            if any(_unsafe_name(entry.filename) for entry in entries):
                return Reason.UNSAFE_ARCHIVE
            total = sum(entry.file_size for entry in entries)
            if total > limits.max_zip_uncompressed_bytes or total > limits.max_zip_ratio * size:
                return Reason.UNSAFE_ARCHIVE
            names = {entry.filename for entry in entries}
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        OSError,
        ValueError,
        NotImplementedError,
        struct.error,
    ):
        return Reason.UNREADABLE_ARCHIVE
    if not all(part in names for part in DOCX_PARTS):
        return Reason.CONTENT_MISMATCH
    return None


def _unsafe_name(name: str) -> bool:
    """A member name that could escape a folder if the archive were ever extracted."""
    parts = name.replace("\\", "/").split("/")
    return name.startswith(("/", "\\")) or ".." in parts or (len(name) > 1 and name[1] == ":")


DEFAULT_LIMITS: Final = Limits()


def validate_file(path: Path, limits: Limits = DEFAULT_LIMITS) -> Verdict:
    """Decide whether ``path`` may be imported. Reads at most the head of the file, plus the
    directory of a zip file. Never raises for a bad file; a refusal is a verdict.

    The check and the copy into the vault are separate reads, and the source can change between
    them. The import pipeline must therefore validate the private copy it is about to keep (or the
    stored blob), not the file it was pointed at.
    """
    try:
        if not path.is_file():
            return _refuse(Reason.NOT_A_FILE)
        size = path.stat().st_size
        if size == 0:
            return _refuse(Reason.EMPTY)
        if size > limits.max_bytes:
            return _refuse(Reason.TOO_LARGE)
        extension = path.suffix.removeprefix(".").lower()
        expected = EXTENSIONS.get(extension)
        if expected is None:
            return _refuse(Reason.UNSUPPORTED_EXTENSION)
        with path.open("rb") as stream:
            head = stream.read(HEAD_BYTES)
        detected = sniff(head)
        if detected is Kind.OLE and expected is Kind.DOCX:
            return _refuse(Reason.LEGACY_OR_ENCRYPTED_OFFICE, detected)
        if detected is Kind.ZIP and expected is Kind.DOCX:
            reason = _check_docx(path, size, limits)
            if reason is not None:
                return _refuse(reason, Kind.ZIP)
            detected = Kind.DOCX
        if detected is not expected:
            return _refuse(Reason.CONTENT_MISMATCH, detected)
    except OSError:
        return _refuse(Reason.UNREADABLE)
    stored = "jpg" if expected is Kind.JPG else extension
    return Verdict(None, detected, stored, MIME_TYPES[(stored, expected.value)])
