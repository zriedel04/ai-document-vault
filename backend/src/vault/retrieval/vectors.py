"""The vector index ``index.db``: one embedding per chunk, and nearest-neighbour search.

``docs/architecture.md`` sections 5, 6 and 8 and ADR 0004: vectors live in a separate SQLite file,
``index.db``, searched with sqlite-vec's exact (brute-force) KNN. ``vault.db`` holds what cannot be
rebuilt; ``index.db`` holds only what can (every vector is made again from the chunks' text by the
embedding stage), so it is left out of backups and may be deleted at any time.

**Layout.** One table ``index_meta`` (key, value) records the layout version (``format``), the
embedding model id and the number of dimensions the index was built with, and one sqlite-vec table
``chunk_vectors`` holds a float32 vector per chunk, keyed by the chunk's id (``chunks.id`` in
``vault.db``, which is never reused), with the chunk's document and version as filter columns. The
distance is cosine distance (1 - cosine similarity, 0 to 2), which does not depend on whether a
model normalises its vectors. One index holds one model: the plan's "one table per model" and
"deleted and rebuilt when switching models" are met by one table per file, and a different model or
dimension count is refused (``IndexModelMismatch``) until the caller rebuilds the index
(``VectorIndex.create``). No embedding model is chosen here (ADR 0013 is Proposed): the model id is
whatever the caller names.

**Damage.** A file that is not a SQLite database, fails ``PRAGMA quick_check``, or does not have
exactly this layout is rebuildable data in a bad state: it is removed with its side files
(``-wal``, ``-shm``, ``-journal``) and an empty index is made in its place, and ``vault.db`` is
never opened or touched. Only those answers count as damage: a database that is busy (locked by
another connection), a disk error or an extension that cannot load is a failure of the setup, and
propagates without removing anything.

**Links.** A symbolic link, a Windows junction or other reparse point, or a file with a second name
(a hard link) where ``index.db`` or one of its side files belongs is refused (``IndexPathError``),
as the blob store refuses one where a blob belongs: SQLite would follow it and write vectors
outside the vault. Nothing is removed in that case; the owner looks at it.

**Deleting.** A permanent delete removes the document's vectors after ``vault.db`` has committed
(``vault.storage.purge``). If that step fails or the process dies first, the vectors of chunks
that no longer exist stay until the next start, where ``sweep`` removes every vector whose chunk,
document and version are not a chunk of ``vault.db`` (chunk ids are never reused, so a stale
vector can never stand for another chunk in between).

**Threads.** A ``VectorIndex`` holds one SQLite connection and is used by one thread at a time.
Opening, creating and removing the file are serialised in this process by one lock, so two threads
cannot both judge a file damaged and remove each other's new index. The server is the only process
that opens a vault (the launcher's lock), so no lock between processes is needed.

Nothing here logs, and no error quotes a path, a vector or anything from a document.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
import struct
import threading
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import sqlite_vec

from vault.db import ids

FORMAT: Final = "1"
"""The layout this module writes. Any other value in a file is damage (an index is rebuildable)."""

TABLE: Final = "chunk_vectors"
MAX_DIMENSIONS: Final = 8192
"""sqlite-vec's own limit for a ``float[N]`` column."""
MAX_K: Final = 4096
"""sqlite-vec's own limit for ``k`` in one KNN query."""
MODEL_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}")
"""A model id names a model and its revision (``org/name@commit``): no spaces, no free text."""
SIDE_SUFFIXES: Final = ("-wal", "-shm", "-journal")
BUSY_TIMEOUT_MS: Final = 5000
FILE_MODE: Final = 0o600
_DAMAGE_CODES: Final = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})
_LOCK = threading.Lock()


class VectorIndexError(RuntimeError):
    """The vector index cannot be used the way it was asked to be."""


class IndexModelMismatch(VectorIndexError):  # noqa: N818 - the name says what it is
    """The index was built with another model or dimension count: rebuild it to use this one."""

    def __init__(self, model: str, dimensions: int) -> None:
        super().__init__(
            "index.db was built with another embedding model or dimension count; rebuild it"
        )
        self.model = model
        self.dimensions = dimensions


class IndexPathError(VectorIndexError):
    """A link, a junction or a file with a second name is where index.db or a side file belongs."""


class VectorIndexUnavailable(VectorIndexError):  # noqa: N818 - the name says what it is
    """This Python's sqlite3 cannot load extensions, or sqlite-vec does not load."""


@dataclass(frozen=True)
class Neighbour:
    """One search result: the chunk and its cosine distance to the query (smaller is nearer)."""

    chunk_id: int
    distance: float


@dataclass(frozen=True)
class ChunkVector:
    """The vector of one chunk, with the document and version the chunk belongs to."""

    chunk_id: int
    document_id: str
    version_id: str
    vector: Sequence[float]


@dataclass(frozen=True)
class Swept:
    """What ``sweep`` did: ``removed`` vectors had no matching chunk; ``rebuilt`` if the file was
    damaged and made empty again."""

    removed: int
    rebuilt: bool


def side_files(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in SIDE_SUFFIXES)


def _check_not_linked(path: Path) -> bool:
    """True if a plain file is at ``path``, False if nothing is; ``IndexPathError`` otherwise."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return False
    reparse = getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if stat.S_ISLNK(status.st_mode) or reparse or not stat.S_ISREG(status.st_mode):
        msg = (
            "something that is not a plain file is where index.db or one of its side files belongs"
        )
        raise IndexPathError(msg)
    if status.st_nlink != 1:
        msg = "index.db or one of its side files has a second name (a hard link)"
        raise IndexPathError(msg)
    return True


def _check_paths(path: Path) -> bool:
    """Refuse a link at the index or a side file; True if the index file exists."""
    for side in side_files(path):
        _check_not_linked(side)
    return _check_not_linked(path)


def _is_damage(error: sqlite3.DatabaseError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in _DAMAGE_CODES


def _remove_files(path: Path) -> None:
    """Remove the index and its side files (the caller holds ``_LOCK``, its connection closed)."""
    for file in (path, *side_files(path)):
        _check_not_linked(file)
        file.unlink(missing_ok=True)


def _create_file(path: Path) -> None:
    """Make an empty, owner-only file (SQLite gives its side files the same mode)."""
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(path, flags, FILE_MODE))


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        try:
            connection.enable_load_extension(True)
            connection.load_extension(sqlite_vec.loadable_path())
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.OperationalError) as error:
            msg = "sqlite-vec cannot be loaded into this Python's sqlite3"
            raise VectorIndexUnavailable(msg) from error
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            msg = "SQLite could not switch index.db to WAL mode"
            raise VectorIndexError(msg)
        connection.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        connection.close()
        raise
    return connection


def _table_sql(dimensions: int) -> str:
    return (
        f"CREATE VIRTUAL TABLE {TABLE} USING vec0(chunk_id integer primary key, "
        f"embedding float[{dimensions}] distance_metric=cosine, "
        "document_id text, version_id text)"
    )


def check_model(model: str, dimensions: int) -> None:
    """Refuse a model id that is not a plain name or a dimension count sqlite-vec cannot hold."""
    if not isinstance(model, str) or MODEL_ID.fullmatch(model) is None:
        msg = "an embedding model id is a name such as org/model@revision"
        raise ValueError(msg)
    if type(dimensions) is not int or not 1 <= dimensions <= MAX_DIMENSIONS:
        msg = f"the dimension count must be a whole number from 1 to {MAX_DIMENSIONS}"
        raise ValueError(msg)


def _layout(connection: sqlite3.Connection) -> tuple[str, int] | None:
    """The model and dimension count of an index in this module's layout; None for an empty
    database; ``_DamagedError`` for anything else."""
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise _DamagedError
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    if not names:
        return None
    if "index_meta" not in names:
        raise _DamagedError
    meta = dict(connection.execute("SELECT key, value FROM index_meta").fetchall())
    if set(meta) != {"format", "model", "dimensions"} or meta["format"] != FORMAT:
        raise _DamagedError
    model, raw_dimensions = meta["model"], meta["dimensions"]
    if (
        not isinstance(raw_dimensions, str)
        or re.fullmatch(r"[1-9][0-9]{0,3}", raw_dimensions) is None
    ):
        raise _DamagedError
    dimensions = int(raw_dimensions)
    try:
        check_model(model, dimensions)
    except ValueError:
        raise _DamagedError from None
    table = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    if table is None or table[0] != _table_sql(dimensions):
        raise _DamagedError
    # The table is read once, so a vec0 table whose own storage is broken shows here too.
    connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()  # noqa: S608 - a constant
    return model, dimensions


class _DamagedError(Exception):
    """The file is rebuildable data in a bad state."""


def _initialise(connection: sqlite3.Connection, model: str, dimensions: int) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "CREATE TABLE index_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO index_meta (key, value) VALUES (?, ?)",
            [("format", FORMAT), ("model", model), ("dimensions", str(dimensions))],
        )
        connection.execute(_table_sql(dimensions))
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _read(path: Path) -> tuple[sqlite3.Connection, tuple[str, int] | None] | None:
    """Connect to the file and read its layout; None if it is damaged (the connection is closed
    then). Any other failure propagates, with the connection closed."""
    try:
        connection = _connect(path)
    except sqlite3.DatabaseError as error:
        if _is_damage(error):
            return None
        raise
    try:
        return connection, _layout(connection)
    except _DamagedError:
        pass
    except sqlite3.DatabaseError as error:
        if not _is_damage(error):
            connection.close()
            raise
    except BaseException:
        connection.close()
        raise
    connection.close()
    return None


def _open(path: Path, wanted: tuple[str, int] | None) -> tuple[sqlite3.Connection, str, int] | None:
    """Open the index at ``path`` (the caller holds ``_LOCK``).

    With ``wanted`` (model, dimensions), a missing, empty or damaged index is made anew for that
    model, and one built for another is refused. Without it, the index is opened whatever its
    model, and None is returned when there is none (nothing is created); a damaged one is removed.
    """
    if not _check_paths(path):
        if wanted is None:
            return None
        _create_file(path)
    read = _read(path)
    if read is None:
        # Rebuildable data in a bad state: remove it, and make it anew once if asked to.
        _remove_files(path)
        if wanted is None:
            return None
        _create_file(path)
        read = _read(path)
        if read is None:  # pragma: no cover - a new empty file is a valid database
            msg = "index.db could not be made anew"
            raise VectorIndexError(msg)
    connection, found = read
    if found is None:
        if wanted is None:
            connection.close()
            return None
        try:
            _initialise(connection, *wanted)
        except BaseException:
            connection.close()
            raise
        return connection, *wanted
    if wanted is not None and found != wanted:
        connection.close()
        raise IndexModelMismatch(*found)
    return connection, *found


def _pack(vector: Sequence[float], dimensions: int) -> bytes:
    """A vector as sqlite-vec's float32 bytes, or ``ValueError`` if it is not one this index can
    rank: wrong length, a value that is not a number (``bool`` is not one), not finite or beyond
    float32, or all zeros (a cosine distance needs a direction)."""
    if isinstance(vector, (str, bytes)) or len(vector) != dimensions:
        msg = f"a vector must have exactly {dimensions} numbers"
        raise ValueError(msg)
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = "a vector holds numbers only"
            raise ValueError(msg)
        if not math.isfinite(value):
            msg = "a vector holds finite numbers only"
            raise ValueError(msg)
    try:
        packed = struct.pack(f"<{dimensions}f", *vector)
    except (OverflowError, struct.error) as error:
        msg = "a vector value is beyond what float32 holds"
        raise ValueError(msg) from error
    floats = struct.unpack(f"<{dimensions}f", packed)
    if not all(math.isfinite(value) for value in floats):
        msg = "a vector value is beyond what float32 holds"  # pragma: no cover - pack refuses it
        raise ValueError(msg)  # pragma: no cover
    if not any(floats):
        msg = "a vector of zeros has no direction to compare"
        raise ValueError(msg)
    return packed


def _chunk_ids(values: Collection[int]) -> str:
    for value in values:
        if type(value) is not int or value < 1:
            msg = "a chunk id is a whole number above 0"
            raise ValueError(msg)
    return json.dumps(sorted(set(values)))


def _ulids(values: Collection[str], what: str) -> str:
    for value in values:
        if not isinstance(value, str) or not ids.is_ulid(value):
            msg = f"a {what} id is a ULID"
            raise ValueError(msg)
    return json.dumps(sorted(set(values)))


class VectorIndex:
    """An open ``index.db`` for one model (see the module docstring)."""

    def __init__(self, connection: sqlite3.Connection, model: str, dimensions: int) -> None:
        self._connection = connection
        self.model = model
        self.dimensions = dimensions

    @classmethod
    def open(cls, path: Path, model: str, dimensions: int) -> Self:
        """Open ``index.db`` for this model, making it if it is missing, empty or damaged.

        ``IndexModelMismatch`` if it was built with another model or dimension count;
        ``IndexPathError`` for a link where it belongs; ``VectorIndexUnavailable`` if sqlite-vec
        does not load.
        """
        check_model(model, dimensions)
        with _LOCK:
            opened = _open(path, (model, dimensions))
        assert opened is not None  # noqa: S101 - with a model, _open makes the index
        return cls(*opened)

    @classmethod
    def create(cls, path: Path, model: str, dimensions: int) -> Self:
        """Remove ``index.db`` whatever it holds and make an empty one for this model: the
        rebuild after a change of model, or of the layout."""
        check_model(model, dimensions)
        with _LOCK:
            _check_paths(path)
            _remove_files(path)
            opened = _open(path, (model, dimensions))
        assert opened is not None  # noqa: S101 - with a model, _open makes the index
        return cls(*opened)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _write(self, statements: Iterable[tuple[str, tuple[object, ...]]]) -> int:
        """Run the statements in one transaction; the number of rows they changed."""
        changed = 0
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for sql, parameters in statements:
                changed += max(self._connection.execute(sql, parameters).rowcount, 0)
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return changed

    def put(self, rows: Iterable[ChunkVector]) -> int:
        """Store each chunk's vector, replacing the one it had, in one transaction. Every row is
        checked before anything is written; ``ValueError`` names no value. The number stored."""
        prepared = []
        for row in rows:
            _chunk_ids([row.chunk_id])
            _ulids([row.document_id], "document")
            _ulids([row.version_id], "version")
            prepared.append(
                (row.chunk_id, _pack(row.vector, self.dimensions), row.document_id, row.version_id)
            )
        if len({row[0] for row in prepared}) != len(prepared):
            msg = "a chunk is given more than one vector"
            raise ValueError(msg)
        statements: list[tuple[str, tuple[object, ...]]] = []
        for chunk_id, packed, document_id, version_id in prepared:
            # sqlite-vec has no INSERT OR REPLACE: delete, then insert, in the same transaction.
            statements.append((f"DELETE FROM {TABLE} WHERE chunk_id = ?", (chunk_id,)))  # noqa: S608
            statements.append(
                (
                    f"INSERT INTO {TABLE} (chunk_id, embedding, document_id, version_id)"  # noqa: S608
                    " VALUES (?, ?, ?, ?)",
                    (chunk_id, packed, document_id, version_id),
                )
            )
        self._write(statements)
        return len(prepared)

    def delete_document(self, document_id: str) -> int:
        """Remove every vector of the document; the number removed."""
        _ulids([document_id], "document")
        return _delete_where(self._connection, "document_id", document_id)

    def delete_version(self, version_id: str) -> int:
        """Remove every vector of the version; the number removed."""
        _ulids([version_id], "version")
        return _delete_where(self._connection, "version_id", version_id)

    def delete_chunks(self, chunk_ids: Collection[int]) -> int:
        """Remove the vectors of these chunks; the number removed."""
        listed = _chunk_ids(chunk_ids)
        return self._write(
            [
                (
                    f"DELETE FROM {TABLE} WHERE chunk_id IN (SELECT value FROM json_each(?))",  # noqa: S608
                    (listed,),
                )
            ]
        )

    def count(self) -> int:
        return int(self._connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])  # noqa: S608

    def search(
        self,
        query: Sequence[float],
        k: int,
        *,
        chunk_ids: Collection[int] | None = None,
        document_ids: Collection[str] | None = None,
    ) -> list[Neighbour]:
        """The ``k`` chunks nearest to ``query``, nearest first (ties by chunk id).

        ``chunk_ids`` and ``document_ids`` limit the search to those chunks and documents before
        ranking (plan section 8: scope first), so the ``k`` are the nearest within the scope. An
        empty collection allows nothing and returns no result; None means no limit.
        """
        if type(k) is not int or not 1 <= k <= MAX_K:
            msg = f"k must be a whole number from 1 to {MAX_K}"
            raise ValueError(msg)
        packed = _pack(query, self.dimensions)
        conditions = ["embedding MATCH ?", "k = ?"]
        parameters: list[object] = [packed, k]
        if chunk_ids is not None:
            if not chunk_ids:
                return []
            conditions.append("chunk_id IN (SELECT value FROM json_each(?))")
            parameters.append(_chunk_ids(chunk_ids))
        if document_ids is not None:
            if not document_ids:
                return []
            conditions.append("document_id IN (SELECT value FROM json_each(?))")
            parameters.append(_ulids(document_ids, "document"))
        rows = self._connection.execute(
            f"SELECT chunk_id, distance FROM {TABLE} WHERE {' AND '.join(conditions)}"  # noqa: S608
            " ORDER BY distance",
            parameters,
        ).fetchall()
        return sorted(
            (Neighbour(int(chunk_id), float(distance)) for chunk_id, distance in rows),
            key=lambda n: (n.distance, n.chunk_id),
        )

    def entries(self) -> set[tuple[int, str, str]]:
        """Every (chunk, document, version) the index holds a vector for."""
        return {
            (int(chunk), str(document), str(version))
            for chunk, document, version in self._connection.execute(
                f"SELECT chunk_id, document_id, version_id FROM {TABLE}"  # noqa: S608
            )
        }


def _delete_where(connection: sqlite3.Connection, column: str, value: str) -> int:
    connection.execute("BEGIN IMMEDIATE")
    try:
        removed = connection.execute(
            f"DELETE FROM {TABLE} WHERE {column} = ?",  # noqa: S608 - a constant column name
            (value,),
        ).rowcount
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    return max(removed, 0)


def _open_any(path: Path) -> VectorIndex | None:
    """The index at ``path`` whatever its model, or None if there is none (a damaged one is
    removed, which removes its vectors too). Nothing is created."""
    with _LOCK:
        opened = _open(path, None)
    return None if opened is None else VectorIndex(*opened)


def remove_document(path: Path, document_id: str) -> int:
    """Remove the document's vectors from ``index.db`` if it exists, whatever model built it; the
    number removed. Used by the permanent delete."""
    _ulids([document_id], "document")
    index = _open_any(path)
    if index is None:
        return 0
    with index:
        return index.delete_document(document_id)


def sweep(path: Path, live: Callable[[], Collection[tuple[int, str, str]]]) -> Swept:
    """Remove every vector whose (chunk, document, version) is not in ``live()``: what a crash
    between a permanent delete's commit and its vector step left, or a chunk replaced since. Run
    at start-up, before any request; ``live`` reads ``vault.db``'s chunks and is called only if
    the index exists."""
    existed = _check_paths(path)
    index = _open_any(path)
    if index is None:
        return Swept(removed=0, rebuilt=existed and not path.exists())
    with index:
        stale = index.entries() - set(live())
        removed = index.delete_chunks({chunk for chunk, _, _ in stale}) if stale else 0
    return Swept(removed=removed, rebuilt=False)
