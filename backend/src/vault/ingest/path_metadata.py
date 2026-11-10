"""Deterministic metadata from where a file was imported from (``docs/architecture.md`` section 7,
step 9, and the metadata rules of section 6): the folders above it become ``folder`` labels, and
its file name gives a title.

Both come from the import source, not from the bytes, so they are written with the source in the
import's transaction (``record_import_labels``) and not by a job: a second import of the same
bytes stops at step 4 of the pipeline and runs no job, yet its folders are new. The title is the
lowest tier of the plan's resolution order (your value > accepted AI > pending AI > file
properties and import path > file name) and is worked out when a document is read, from the name
that names the document (its first import), so it can never disagree with that name.

Every rule here is fixed text processing, the same on every machine and offline. Folder and file
names are the owner's own words and are treated as untrusted text: nothing here logs, and what a
name may hold is cut down to what a label can show (``clean``):

- Unicode NFC, so a name typed on one system and copied from another is one name.
- Control characters, unpaired surrogates, the bidirectional controls that can make a name look
  like another (U+202E and friends) and invisible characters (U+200B, U+FEFF and friends) are
  removed; every run of white space becomes one space.
- A folder name loses trailing dots and spaces, as Windows drops them, and a name that is then
  empty (``.``, ``..``, ``...``) is no folder. Longer names are cut to ``MAX_LABEL_CHARS``.

``folder_names`` reads the recorded folders as Windows reads a path: ``\\`` and ``/`` both
separate folders, and a drive (``C:``), a UNC share (``\\\\server\\share``) or a device prefix
(``\\\\?\\``, ``\\\\.\\``) at the start is where the path is, not a folder in it, so it is not a
label. The importer stores the folders as its caller gives them; the folder import (a later phase)
is to give them from the folder the owner chose, so ``Users`` and the account name are not labels.
A browser upload records no folders and so gets no labels.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Connection, Engine, delete, func, select

from vault.db.engine import write_transaction
from vault.db.labels import add_folder_labels
from vault.db.schema import MAX_LABEL_CHARS, app_meta, import_sources

# How many folders deep labels go; deeper folders are left out (the outermost are kept, since each
# label hangs under the one above it). Windows' classic 260-character path limit allows about 130.
MAX_DEPTH: Final = 64
# The marker migration 0006 leaves for sources recorded before folder labels existed.
PENDING_KEY: Final = "pending.folder_labels"
# The source of a title worked out from a file name (the lowest tier of the resolution order).
FILENAME: Final = "filename"

# Characters a label never keeps: the bidirectional controls, which reorder how the text around
# them is shown, and the invisible ones that would make two names that look the same differ (soft
# hyphen, zero-width space, word joiners, byte order mark). The zero-width joiners (U+200C,
# U+200D) stay: some scripts and emoji need them.
_HIDDEN: Final = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    "\u00ad\u180e\u200b\u2060\u2061\u2062\u2063\u2064\ufeff"
)
_SEPARATORS: Final = re.compile(r"[\\/]+")
_DRIVE: Final = re.compile(r"[A-Za-z]:")


def clean(text: str) -> str:
    """``text`` as a label may show it: NFC, without control, bidirectional or invisible characters
    (``_HIDDEN``), with each
    run of white space (a line break and a tab included) as one space and none at either end."""
    kept = "".join(
        " " if character.isspace() else character
        for character in text
        if character not in _HIDDEN
        and (character.isspace() or unicodedata.category(character) not in {"Cc", "Cs"})
    )
    return " ".join(unicodedata.normalize("NFC", kept).split())


def _folder_name(text: str) -> str:
    name = clean(text).rstrip(". ")
    return name[:MAX_LABEL_CHARS].rstrip(". ")


def _drop_anchor(parts: list[str], *, unc: bool) -> list[str]:
    """``parts`` without the drive, share or device the path starts at."""
    if unc:  # \\server\share\..., \\?\C:\..., \\?\UNC\server\share\..., \\.\device\...
        if parts and parts[0] in {"?", "."}:
            device = parts[1:]
            if device and device[0].upper() == "UNC":
                return device[3:]
            return device[1:]
        return parts[2:]
    if parts and _DRIVE.match(parts[0]):  # C:\... and the drive-relative C:folder\...
        rest = parts[0][2:]
        return [rest, *parts[1:]] if rest else parts[1:]
    return parts  # a lone leading separator is the root of the current drive: nothing to drop


def folder_names(segments: Sequence[str]) -> tuple[str, ...]:
    """The folder labels for an import's recorded folders, outermost first (see the module)."""
    path = "\\".join(segments)
    unc = re.match(r"[\\/]{2}", path) is not None
    parts = [part for part in _SEPARATORS.split(path) if part]
    names = (_folder_name(part) for part in _drop_anchor(parts, unc=unc))
    return tuple(name for name in names if name)[:MAX_DEPTH]


def title_from_filename(filename: str) -> str | None:
    """A title from a file name: the name without its extension, with underscores read as spaces
    (``Lab_report_3.pdf`` is "Lab report 3"), or None when nothing is left."""
    stem, dot, _extension = filename.rpartition(".")
    if not dot or not stem.strip(". "):  # no extension, or a name such as ".pdf"
        stem = filename
    title = clean(stem.replace("_", " "))[:MAX_LABEL_CHARS].strip()
    return title or None


def resolved_title(first_filename: object) -> tuple[str, str] | None:
    """The title a document shows and where it came from (``FILENAME``, the only tier written so
    far), from the file name of its first import, or None. The detail panel and the export both
    read it here, so they never disagree. A name that is not text (only a hand edit writes one)
    gives no title."""
    if not isinstance(first_filename, str):
        return None
    value = title_from_filename(first_filename)
    return None if value is None else (value, FILENAME)


def record_import_labels(
    connection: Connection, document_id: str, segments: Sequence[str], now: datetime
) -> int:
    """Give a document the folder labels of one import, in the caller's transaction. Returns how
    many label assignments were new."""
    return add_folder_labels(connection, document_id, folder_names(segments), now)


@dataclass(frozen=True)
class Backfill:
    added: int  # label assignments that were new
    skipped: int  # sources whose folders are not a list of names (only a hand edit writes one)


def label_pending_sources(engine: Engine, now: datetime) -> Backfill | None:
    """Label every import source recorded before folder labels existed, once: only while the
    marker of migration 0006 is there, and the marker is removed in the same transaction. None
    when nothing was pending. A source whose folders are not a list of names is skipped and
    counted, rather than stopping every start."""
    with write_transaction(engine) as connection:
        pending = connection.execute(
            select(app_meta.c.key).where(app_meta.c.key == PENDING_KEY)
        ).first()
        if pending is None:
            return None
        added = skipped = 0
        sources = connection.execute(
            select(import_sources.c.document_id, import_sources.c.folder_segments)
            .where(func.json_array_length(import_sources.c.folder_segments) > 0)
            .order_by(import_sources.c.imported_at, import_sources.c.id)
        ).all()
        for source in sources:
            folders = json.loads(source.folder_segments)
            if not isinstance(folders, list) or not all(isinstance(name, str) for name in folders):
                skipped += 1
                continue
            added += record_import_labels(connection, source.document_id, folders, now)
        connection.execute(delete(app_meta).where(app_meta.c.key == PENDING_KEY))
        return Backfill(added=added, skipped=skipped)
