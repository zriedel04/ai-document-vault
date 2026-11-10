"""Export the vault into a normal folder tree (``docs/architecture.md``, "Avoiding lock-in").

    <target>/
      files/<folders of the first import>/<name of the first import>   each current original
      manifest.json   every exported document: states, title, labels, versions, import sources,
                      hashes, sizes
      manifest.csv    the same, one row per import source, for a spreadsheet

The layout follows the first import (earliest, ties broken by id: the rule that names a document
in the library); collections do not exist yet. Each document's title is the one the library
shows, with its source (``vault.ingest.path_metadata.resolved_title``), and its labels are listed
in the library's order (``vault.db.labels.label_order``) with their full path. Documents in the
trash are left out unless ``include_trash`` is set. Names are untrusted text and are made safe as
Windows path components (:func:`safe_name`), every path stays within ``MAX_PATH``, and names that
collide in a folder, compared without regard to case, are numbered in a fixed order.
``manifest.csv`` is UTF-8 with a byte order mark and neutralises cells that a spreadsheet would
run as formulas (:func:`neutralise`); its ``labels`` cell is the JSON list of ``manifest.json``,
which escapes every line break, and ``manifest.json`` holds the exact values.
``backend/README.md`` (section Export) gives every rule.

The export is built in a new folder beside the target and renamed to it only when complete, so a
folder under the target's name is always a whole export; the target must be new or empty, and is
checked by the backup's rules (``vault.storage.backup.check_target``). Each original is hashed as
it is copied and kept only if its hash and exact size match its record. Messages are fixed text:
no file name, hash or path appears in them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from vault.db.labels import label_order, label_path
from vault.ingest.path_metadata import PENDING_KEY, resolved_title
from vault.storage.backup import (
    BackupError,
    _secure_target,
    _tree_is_private,
    check_target,
)
from vault.storage.blobs import (
    CHUNK_BYTES,
    EXTENSION,
    SHA256,
    BlobIntegrityError,
    BlobStore,
    _is_real_dir,
    _is_real_file,
    _lstat,
)
from vault.storage.layout import VaultPaths

FILES: Final = "files"
MANIFEST_JSON: Final = "manifest.json"
MANIFEST_CSV: Final = "manifest.csv"
TEMP_PREFIX: Final = ".vault-export-"
TEMP_NAME_UNITS: Final = len(TEMP_PREFIX) + 8  # tempfile.mkdtemp adds eight characters
FORMAT: Final = "ai-document-vault-export"
FORMAT_VERSION: Final = 2  # 2: title and labels
TRASHED: Final = "trashed"

# Windows path limits, in UTF-16 code units, as Windows counts them. MAX_PATH is 260 with the
# terminating NUL; a folder must leave room for an 8.3 file name in it (CreateDirectory).
MAX_FILE_PATH: Final = 259
MAX_FOLDER_PATH: Final = 247
COMPONENT_UNITS: Final = 120  # NTFS allows 255; shorter keeps room for the folders above
MIN_NAME_UNITS: Final = 40  # a file name always gets at least this much room
MIN_FOLDER_UNITS: Final = 8  # a folder shorter than this is left out instead
EXTENSION_UNITS: Final = 16  # a longer "extension" is treated as part of the name

FORBIDDEN: Final = frozenset('<>:"/\\|?*')
DEVICES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{digit}" for port in ("COM", "LPT") for digit in "0123456789\u00b9\u00b2\u00b3"}
)
# A spreadsheet reads a cell that starts with one of these as a formula (OWASP, CSV injection);
# the full-width forms are included because some spreadsheets convert them.
FORMULA_STARTS: Final = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")

# The columns of manifest.csv: the document's, its current version's, then one import source's.
DOCUMENT_COLUMNS: Final = (
    "id", "file", "problem", "state", "state_before_trash", "local_only", "created_at", "trashed_at"
)  # fmt: skip
# The document's title, the title's source and its labels (a JSON list, as in manifest.json).
DESCRIPTION_COLUMNS: Final = ("title", "title_source", "labels")
VERSION_COLUMNS: Final = ("id", "version_no", "sha256", "size", "extension", "mime")
SOURCE_COLUMNS: Final = (
    "id", "original_filename", "original_path", "folder_segments", "original_mtime",
    "imported_at", "method", "import_batch_id",
)  # fmt: skip
CSV_COLUMNS: Final = (
    "document_id", *DOCUMENT_COLUMNS[1:], *DESCRIPTION_COLUMNS, "version_id", *VERSION_COLUMNS[1:],
    "source_no", "source_id", *SOURCE_COLUMNS[1:],
)  # fmt: skip


class ExportError(Exception):
    """The export cannot go on. The message is fixed text and names no file or folder."""


@dataclass(frozen=True)
class ExportResult:
    exported: int  # documents whose original was written
    missing: int  # documents whose original the blob store does not hold
    damaged: int  # documents whose original or record does not match its hash, not written
    unreadable: int  # documents whose original could not be read, not written
    trash_left_out: int  # documents in the trash, left out (include_trash not set)
    renamed: int  # documents given a numbered name because another took theirs
    placed_higher: int  # documents placed above their folder to keep the path short
    leftovers: int  # folders of earlier exports that did not finish, beside the target
    labels_complete: bool  # False: the app has not yet labelled the imports of an older vault
    private: bool | None  # Windows: is the export, all of it, owner-only? None elsewhere

    @property
    def problems(self) -> int:
        return self.missing + self.damaged + self.unreadable


def units(text: str) -> int:
    """The length of ``text`` as Windows counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _cut(text: str, limit: int) -> str:
    """The longest start of ``text`` that fits in ``limit`` units, never half a character."""
    kept, used = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit:
            break
        kept.append(char)
        used += width
    return "".join(kept)


def _clean(text: str) -> str:
    """``text`` with every character Windows refuses in a name replaced, and without the trailing
    dots and spaces Windows drops (so two names that Windows would make one stay apart)."""
    chars = (
        "_" if char in FORBIDDEN or ord(char) < 0x20 or 0xD800 <= ord(char) <= 0xDFFF else char
        for char in text
    )
    return "".join(chars).rstrip(" .")


def is_device_name(name: str) -> bool:
    """Does Windows read ``name`` as a device? The part before the first dot counts, without
    trailing spaces, in any case: ``nul.txt`` and ``Com1 .tar.gz`` are devices too."""
    return name.split(".", 1)[0].rstrip(" ").upper() in DEVICES


def _split(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot > 0 and units(name[dot:]) <= EXTENSION_UNITS:
        return name[:dot], name[dot:]
    return name, ""


def safe_name(
    stem: str, extension: str = "", suffix: str = "", limit: int = COMPONENT_UNITS
) -> str:
    """A safe Windows name from a cleaned stem and extension, with ``suffix`` (`` (2)``) between
    them, cut to ``limit`` units (the stem is shortened; the extension and suffix are kept)."""
    room = limit - units(suffix) - units(extension) - 1  # one unit for a device name's "_"
    name = (_cut(stem, room) + suffix + extension).rstrip(" .") or "_"
    return "_" + name if is_device_name(name) else name


@dataclass
class _Folder:
    """A folder of the export being built: its path and the names in it, by their case-folded
    form (Windows compares names without regard to case)."""

    path: Path
    rel: tuple[str, ...]
    taken: set[str] = field(default_factory=set)
    children: dict[str, _Folder] = field(default_factory=dict)

    @property
    def rel_units(self) -> int:
        return units("\\".join(self.rel))


class _Layout:
    """Places documents in the export's ``files/`` tree, creating folders and files as it goes.

    Names are claimed in the order documents are placed, so the same vault gives the same paths.
    A folder of the owner's that several documents share is made once and reused; a folder name
    that a file already holds (or the other way round) gets a number."""

    def __init__(self, root: Path, base_units: int) -> None:
        self.file_room = MAX_FILE_PATH - base_units - 1  # what is left below the export folder
        self.folder_room = MAX_FOLDER_PATH - base_units - 1
        root.mkdir()
        self.root = _Folder(root, (FILES,))

    def _claim(
        self, folder: _Folder, stem: str, extension: str, limit: int
    ) -> Iterable[tuple[str, bool]]:
        """Free names in ``folder``, the unnumbered one first, each claimed as it is offered, and
        whether it is numbered."""
        number = 1
        while True:
            suffix = "" if number == 1 else f" ({number})"
            name = safe_name(stem, extension, suffix, limit)
            if name.casefold() not in folder.taken:
                folder.taken.add(name.casefold())
                yield name, number > 1
            number += 1

    def _child(self, folder: _Folder, segment: str) -> _Folder | None:
        """The export folder for one of the owner's folders inside ``folder``, or None if a new
        one would leave too little room for the file name."""
        wanted = _clean(segment) or "_"
        key = wanted.casefold()
        if key in folder.children:
            return folder.children[key]
        limit = min(
            COMPONENT_UNITS,
            self.folder_room - folder.rel_units - 1,
            self.file_room - folder.rel_units - 1 - MIN_NAME_UNITS - 1,
        )
        if limit < MIN_FOLDER_UNITS:
            return None
        for name, _ in self._claim(folder, wanted, "", limit):
            try:
                (folder.path / name).mkdir()
            except FileExistsError:
                continue  # the file system folds names this rule keeps apart; take the next
            child = _Folder(folder.path / name, (*folder.rel, name))
            folder.children[key] = child
            return child
        raise AssertionError  # pragma: no cover - _claim never ends

    def create(self, segments: list[str], filename: str) -> tuple[_Folder, str, int, bool, bool]:
        """Create an empty file for a document; return its folder, name, open descriptor, whether
        it was numbered, and whether it was placed above its own folder."""
        folder, higher = self.root, False
        for segment in segments:
            child = self._child(folder, segment)
            if child is None:
                higher = True
                break
            folder = child
        stem, extension = _split(_clean(filename) or "_")
        limit = min(COMPONENT_UNITS, self.file_room - folder.rel_units - 1)
        for name, numbered in self._claim(folder, stem, extension, limit):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            try:
                fd = os.open(folder.path / name, flags, 0o600)
            except FileExistsError:
                continue  # the file system folds names this rule keeps apart; take the next
            return folder, name, fd, numbered, higher
        raise AssertionError  # pragma: no cover - _claim never ends


class _SourceReadError(Exception):
    """Reading an original from the vault failed (as opposed to writing it into the export)."""


def _copy(source: Path, fd: int, size: int) -> str:
    """Copy at most ``size + 1`` bytes from ``source`` into the open file; return their SHA-256.
    A failure to read is ``_SourceReadError``; a failure to write is the system's own error."""
    digest = hashlib.sha256()
    with os.fdopen(fd, "wb") as out:
        try:
            stream = source.open("rb")
        except OSError as error:
            raise _SourceReadError from error
        with stream:
            left = size + 1
            while left > 0:
                try:
                    chunk = stream.read(min(CHUNK_BYTES, left))
                except OSError as error:
                    raise _SourceReadError from error
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                left -= len(chunk)
        out.flush()
        os.fsync(out.fileno())
        written = out.tell()
    return digest.hexdigest() if written == size else ""


def _time(value: object) -> object:
    """A stored time (UTC without a zone, as the app writes it) as ISO 8601 with ``Z``; any other
    value is kept as it is, so nothing the database holds is lost."""
    if not isinstance(value, str):
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return moment.isoformat().replace("+00:00", "Z")


def _segments(raw: object) -> tuple[object, list[str]]:
    """The stored folder list as the manifest shows it, and the folders to place the file in
    (none unless it is a JSON list of names)."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return raw, []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value, value
    return raw, []


@dataclass(frozen=True)
class _Rows:
    documents: list[sqlite3.Row]
    versions: list[sqlite3.Row]
    sources: list[sqlite3.Row]
    labels: list[sqlite3.Row]  # every label: id, kind, name, parent_id
    assignments: list[sqlite3.Row]  # document_id, label_id, source, status
    labels_complete: bool


def _read_labels(connection: sqlite3.Connection) -> tuple[list[Any], list[Any], bool]:
    """Every label and assignment, and whether they are complete: a database the app has not
    migrated to labels yet has none, and one whose imports the app has not labelled yet (the
    marker of migration 0006, ``PENDING_KEY``) has only those of later imports."""
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not {"labels", "document_labels", "app_meta"} <= tables:
        return [], [], False
    labels = connection.execute("SELECT id, kind, name, parent_id FROM labels").fetchall()
    assignments = connection.execute(
        "SELECT document_id, label_id, source, status FROM document_labels"
    ).fetchall()
    pending = connection.execute("SELECT 1 FROM app_meta WHERE key = ?", (PENDING_KEY,)).fetchone()
    return labels, assignments, pending is None


def _read_database(db: Path) -> _Rows:
    """Documents, versions with their blob records, import sources and labels, all from one
    read-only snapshot (the database is in WAL mode, so the app can go on writing)."""
    if not _is_real_file(_lstat(db)):
        msg = "the vault folder has no database yet; start the app once before exporting it"
        raise ExportError(msg)
    try:
        connection = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            documents = connection.execute(
                "SELECT id, current_version_id, state, state_before_trash, local_only, "
                "created_at, trashed_at FROM documents ORDER BY created_at, id"
            ).fetchall()
            versions = connection.execute(
                "SELECT v.id, v.document_id, v.version_no, v.origin, v.created_at, "
                "v.blob_sha256, b.size, b.ext, b.mime FROM document_versions v "
                "LEFT JOIN blobs b ON b.sha256 = v.blob_sha256 ORDER BY v.document_id, v.version_no"
            ).fetchall()
            sources = connection.execute(
                "SELECT id, document_id, original_filename, original_path, folder_segments, "
                "original_mtime, imported_at, method, import_batch_id FROM import_sources "
                "ORDER BY document_id, imported_at, id"
            ).fetchall()
            labels, assignments, complete = _read_labels(connection)
            connection.execute("COMMIT")
        finally:
            connection.close()
    except sqlite3.Error:
        msg = "SQLite could not read the vault database; nothing was exported"
        raise ExportError(msg) from None
    return _Rows(documents, versions, sources, labels, assignments, complete)


def _version_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "version_no": row["version_no"],
        "origin": row["origin"],
        "created_at": _time(row["created_at"]),
        "sha256": row["blob_sha256"],
        "size": row["size"],
        "extension": row["ext"],
        "mime": row["mime"],
    }


def _source_entry(row: sqlite3.Row) -> dict[str, Any]:
    shown, _ = _segments(row["folder_segments"])
    return {
        "id": row["id"],
        "original_filename": row["original_filename"],
        "original_path": row["original_path"],
        "folder_segments": shown,
        "original_mtime": _time(row["original_mtime"]),
        "imported_at": _time(row["imported_at"]),
        "method": row["method"],
        "import_batch_id": row["import_batch_id"],
    }


def _label_entries(rows: _Rows) -> dict[str, list[dict[str, Any]]]:
    """Each document's labels as the manifest lists them, in the library's order. An assignment
    whose label is gone (only a hand edit with foreign keys off leaves one) keeps its id, source
    and status, with no kind, name or path."""
    known = {row["id"]: row for row in rows.labels}
    tree = {row["id"]: (row["name"], row["parent_id"]) for row in rows.labels}
    of: dict[str, list[dict[str, Any]]] = {}
    for assignment in rows.assignments:
        label = known.get(assignment["label_id"])
        of.setdefault(assignment["document_id"], []).append(
            {
                "id": assignment["label_id"],
                "kind": None if label is None else label["kind"],
                "name": None if label is None else label["name"],
                "path": [] if label is None else list(label_path(tree, label["id"])),
                "source": assignment["source"],
                "status": assignment["status"],
            }
        )
    for entries in of.values():
        entries.sort(key=lambda e: label_order(e["kind"], e["path"], e["source"], e["id"]))
    return of


def _title_entry(sources: list[sqlite3.Row]) -> dict[str, str] | None:
    resolved = resolved_title(sources[0]["original_filename"]) if sources else None
    return None if resolved is None else {"value": resolved[0], "source": resolved[1]}


def _blob_ok(version: sqlite3.Row) -> bool:
    """The record is checked again as it is read back, as the backup checks it."""
    sha256, extension, size = version["blob_sha256"], version["ext"], version["size"]
    return (
        isinstance(sha256, str)
        and SHA256.fullmatch(sha256) is not None
        and isinstance(extension, str)
        and EXTENSION.fullmatch(extension) is not None
        and type(size) is int
        and size >= 0
    )


def neutralise(cell: object) -> str:
    """A CSV cell that a spreadsheet shows as text: one that would start a formula gets a ``'``."""
    text = "" if cell is None else str(cell)
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def csv_bytes(documents: list[dict[str, Any]]) -> bytes:
    """``manifest.csv``: one row per import source (one row for a document without any), UTF-8
    with a byte order mark, every cell neutralised."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for document in documents:
        current: dict[str, Any] = next(
            (v for v in document["versions"] if v["id"] == document["current_version_id"]), {}
        )
        title = document["title"] or {}
        description = [
            title.get("value"),
            title.get("source"),
            json.dumps(document["labels"], ensure_ascii=False),
        ]
        head = [
            *(document[c] for c in DOCUMENT_COLUMNS),
            *description,
            *(current.get(c) for c in VERSION_COLUMNS),
        ]
        for number, source in enumerate(document["sources"] or [None], start=1):
            tail: list[object] = [None] * (1 + len(SOURCE_COLUMNS))
            if source is not None:
                shown = {
                    **source,
                    "folder_segments": json.dumps(source["folder_segments"], ensure_ascii=False),
                }
                tail = [number, *(shown[c] for c in SOURCE_COLUMNS)]
            writer.writerow([neutralise(cell) for cell in (*head, *tail)])
    return buffer.getvalue().encode("utf-8-sig")


def _write_new(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _count_leftovers(parent: Path, mine: Path) -> int:
    return sum(
        1
        for entry in parent.iterdir()
        if entry != mine and entry.name.startswith(TEMP_PREFIX) and _is_real_dir(_lstat(entry))
    )


def _check_empty_target(target: Path) -> None:
    status = _lstat(target)
    if status is None:
        return
    if not _is_real_dir(status):
        msg = "something that is not a folder is where the export folder belongs"
        raise ExportError(msg)
    if any(target.iterdir()):
        msg = "the export folder is not empty; name a new or empty folder"
        raise ExportError(msg)


@dataclass
class _Counts:
    exported: int = 0
    missing: int = 0
    damaged: int = 0
    unreadable: int = 0
    trash_left_out: int = 0
    renamed: int = 0
    placed_higher: int = 0


def _export_documents(
    layout: _Layout, store: BlobStore, rows: _Rows, *, trash: bool
) -> tuple[list[dict[str, Any]], _Counts]:
    documents, versions, sources = rows.documents, rows.versions, rows.sources
    labels_of = _label_entries(rows)
    by_document: dict[str, list[sqlite3.Row]] = {}
    for version in versions:
        by_document.setdefault(version["document_id"], []).append(version)
    sources_of: dict[str, list[sqlite3.Row]] = {}
    for source in sources:
        sources_of.setdefault(source["document_id"], []).append(source)
    counts = _Counts()
    entries: list[dict[str, Any]] = []
    for document in documents:
        if document["state"] == TRASHED and not trash:
            counts.trash_left_out += 1
            continue
        own_versions = by_document.get(document["id"], [])
        own_sources = sources_of.get(document["id"], [])
        current = next((v for v in own_versions if v["id"] == document["current_version_id"]), None)
        file, problem = None, None
        if current is None or not _blob_ok(current):
            problem = "damaged"
        else:
            try:
                found = store.find(current["blob_sha256"])
            except BlobIntegrityError:
                found, problem = None, "damaged"
            if found is None and problem is None:
                problem = "missing"
            if found is not None:
                if own_sources:
                    _, segments = _segments(own_sources[0]["folder_segments"])
                    filename = str(own_sources[0]["original_filename"])
                else:
                    segments, filename = [], f"{document['id']}.{current['ext']}"
                folder, name, fd, numbered, higher = layout.create(segments, filename)
                try:
                    digest = _copy(found, fd, current["size"])
                except _SourceReadError:
                    problem = "unreadable"
                else:
                    if digest != current["blob_sha256"]:
                        problem = "damaged"
                if problem is None:
                    file = "/".join((*folder.rel, name))
                    counts.renamed += int(numbered)
                    counts.placed_higher += int(higher)
                else:
                    (folder.path / name).unlink()
                    folder.taken.discard(name.casefold())
        if problem is None:
            counts.exported += 1
        else:
            setattr(counts, problem, getattr(counts, problem) + 1)
        entries.append(
            {
                "id": document["id"],
                "file": file,
                "problem": problem,
                "state": document["state"],
                "state_before_trash": document["state_before_trash"],
                "local_only": bool(document["local_only"]),
                "created_at": _time(document["created_at"]),
                "trashed_at": _time(document["trashed_at"]),
                "title": _title_entry(own_sources),
                "labels": labels_of.get(document["id"], []),
                "current_version_id": document["current_version_id"],
                "versions": [_version_entry(v) for v in own_versions],
                "sources": [_source_entry(s) for s in own_sources],
            }
        )
    return entries, counts


def run_export(
    vault_root: Path,
    target: Path,
    *,
    include_trash: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    secure: Callable[[Path, bool], bool | None] = _secure_target,
    tree: Callable[[Path], bool] = _tree_is_private,
) -> ExportResult:
    """Export the vault at ``vault_root`` into ``target`` (see the module docstring)."""
    try:
        target = check_target(target, vault_root)
    except BackupError as error:
        # The backup's own checks, in its fixed text; only the folder's role is renamed.
        raise ExportError(str(error).replace("backup target", "export folder")) from None
    parent = target.parent
    if parent == target:
        msg = "the export folder cannot be a drive's top folder; name a folder on it"
        raise ExportError(msg)
    base_units = max(units(str(target)), units(str(parent)) + 1 + TEMP_NAME_UNITS)
    if base_units > MAX_FOLDER_PATH - 1 - len(FILES) - 1 - MIN_NAME_UNITS:
        msg = (
            "the export folder's path is too long to leave room for file names; name a shorter one"
        )
        raise ExportError(msg)
    _check_empty_target(target)
    if _lstat(parent) is None:
        parent.mkdir(parents=True, exist_ok=True)
    if not _is_real_dir(_lstat(parent)):
        msg = (
            "something that is not a folder (or a link) is where the export folder's parent belongs"
        )
        raise ExportError(msg)
    rows = _read_database(VaultPaths(vault_root).db)
    building = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent))
    try:
        private = secure(building, True)
        layout = _Layout(building / FILES, base_units)
        entries, counts = _export_documents(
            layout, BlobStore(VaultPaths(vault_root).blobs), rows, trash=include_trash
        )
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "exported_at": now().astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "trash_included": include_trash,
            "labels_complete": rows.labels_complete,
            "documents": entries,
        }
        text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        _write_new(building / MANIFEST_JSON, text.encode("utf-8"))
        _write_new(building / MANIFEST_CSV, csv_bytes(entries))
        _check_empty_target(target)
        try:
            if _lstat(target) is not None:
                target.rmdir()  # empty, checked just now; a rename onto it fails on Windows
            building.rename(target)
        except OSError:
            msg = "something appeared in the export folder during the export; nothing was replaced"
            raise ExportError(msg) from None
    except BaseException:
        with suppress(OSError):
            shutil.rmtree(building)
        raise
    leftovers = _count_leftovers(parent, target)
    if private:
        private = tree(target)
    return ExportResult(
        exported=counts.exported,
        missing=counts.missing,
        damaged=counts.damaged,
        unreadable=counts.unreadable,
        trash_left_out=counts.trash_left_out,
        renamed=counts.renamed,
        placed_higher=counts.placed_higher,
        leftovers=leftovers,
        labels_complete=rows.labels_complete,
        private=private,
    )
