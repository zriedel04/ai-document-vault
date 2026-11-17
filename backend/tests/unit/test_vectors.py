"""The vector index ``index.db``: round trip, ranking as an ordered list, scope filters, deletes,
one model per index, damage made good without touching ``vault.db``, and links refused."""

from __future__ import annotations

import importlib
import math
import os
import random
import sqlite3
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlite_vec

from vault.db import ids
from vault.retrieval import vectors
from vault.retrieval.vectors import (
    ChunkVector,
    IndexModelMismatch,
    IndexPathError,
    Neighbour,
    VectorIndex,
    VectorIndexUnavailable,
)

MODEL = "synthetic/test-model@0123abc"
DIMS = 4
DOC_A = ids.new_ulid()
DOC_B = ids.new_ulid()
VER_A = ids.new_ulid()
VER_A2 = ids.new_ulid()
VER_B = ids.new_ulid()
SQLITE_HEADER = b"SQLite format 3\x00"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "index.db"


@pytest.fixture
def index(path: Path) -> Iterator[VectorIndex]:
    with VectorIndex.open(path, MODEL, DIMS) as opened:
        yield opened


def row(chunk_id: int, vector: list[float], doc: str = DOC_A, ver: str = VER_A) -> ChunkVector:
    return ChunkVector(chunk_id, doc, ver, vector)


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return 1 - dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def link(target: Path, name: Path) -> None:
    try:
        os.symlink(target, name)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make symbolic links here")


# --- round trip and ranking --------------------------------------------------------------------


def test_a_stored_vector_is_found_with_its_distance(index: VectorIndex) -> None:
    assert index.put([row(1, [1, 0, 0, 0]), row(2, [0, 1, 0, 0], DOC_B, VER_B)]) == 2
    assert index.count() == 2
    assert index.entries() == {(1, DOC_A, VER_A), (2, DOC_B, VER_B)}
    assert index.search([1, 0, 0, 0], 2) == [Neighbour(1, 0.0), Neighbour(2, 1.0)]


def test_the_index_keeps_its_vectors_when_reopened(path: Path) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put([row(7, [0.5, 0.5, 0, 0])])
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.entries() == {(7, DOC_A, VER_A)}
        assert [n.chunk_id for n in index.search([1, 1, 0, 0], 1)] == [7]


def test_the_ranking_is_the_ordered_list_of_an_exact_search(index: VectorIndex) -> None:
    # A plain-Python cosine search over seeded random vectors gives the expected ranked list; the
    # comparison is of the ordered list, so the right ten in another order fails.
    generator = random.Random(20261117)  # noqa: S311 - synthetic test data, not a secret
    stored = {chunk: [generator.uniform(-1, 1) for _ in range(DIMS)] for chunk in range(1, 201)}
    index.put([row(chunk, vector) for chunk, vector in stored.items()])
    for _ in range(5):
        query = [generator.uniform(-1, 1) for _ in range(DIMS)]
        expected = sorted(stored, key=lambda c: (cosine_distance(stored[c], query), c))[:10]
        found = index.search(query, 10)
        assert [n.chunk_id for n in found] == expected
        distances = [n.distance for n in found]
        assert distances == sorted(distances)
        for neighbour in found:
            assert neighbour.distance == pytest.approx(
                cosine_distance(stored[neighbour.chunk_id], query), abs=1e-5
            )


def test_equal_distances_are_ordered_by_chunk_id(index: VectorIndex) -> None:
    index.put([row(9, [1, 0, 0, 0]), row(3, [2, 0, 0, 0]), row(5, [0, 1, 0, 0])])
    assert [n.chunk_id for n in index.search([1, 0, 0, 0], 3)] == [3, 9, 5]


def test_putting_a_chunk_again_replaces_its_vector(index: VectorIndex) -> None:
    index.put([row(1, [1, 0, 0, 0])])
    index.put([row(1, [0, 1, 0, 0], ver=VER_A2)])
    assert index.count() == 1
    assert index.entries() == {(1, DOC_A, VER_A2)}
    assert index.search([0, 1, 0, 0], 1) == [Neighbour(1, 0.0)]


# --- scope filters -----------------------------------------------------------------------------


@pytest.fixture
def scoped(index: VectorIndex) -> VectorIndex:
    # The three nearest to the query are all in document A; B's chunks are farther.
    index.put(
        [
            row(1, [1, 0, 0, 0]),
            row(2, [1, 0.1, 0, 0]),
            row(3, [1, 0.2, 0, 0]),
            row(4, [0, 1, 0, 0], DOC_B, VER_B),
            row(5, [0, 0, 1, 0], DOC_B, VER_B),
        ]
    )
    return index


def test_a_document_filter_ranks_within_the_scope(scoped: VectorIndex) -> None:
    # Not the global top 2 filtered afterwards (which would be empty), but B's nearest two.
    assert [n.chunk_id for n in scoped.search([1, 0, 0, 0], 2, document_ids={DOC_B})] == [4, 5]


def test_a_chunk_filter_ranks_within_the_scope(scoped: VectorIndex) -> None:
    assert [n.chunk_id for n in scoped.search([1, 0, 0, 0], 5, chunk_ids={5, 3})] == [3, 5]


def test_both_filters_must_hold(scoped: VectorIndex) -> None:
    found = scoped.search([1, 0, 0, 0], 5, chunk_ids={1, 4}, document_ids={DOC_B})
    assert [n.chunk_id for n in found] == [4]


def test_an_empty_scope_allows_nothing(scoped: VectorIndex) -> None:
    # An empty set is a scope with no document in it, never "no filter".
    assert scoped.search([1, 0, 0, 0], 5, chunk_ids=set()) == []
    assert scoped.search([1, 0, 0, 0], 5, document_ids=[]) == []
    assert scoped.search([1, 0, 0, 0], 5, document_ids={ids.new_ulid()}) == []


# --- deletes -----------------------------------------------------------------------------------


def test_deleting_a_document_removes_only_its_vectors(scoped: VectorIndex) -> None:
    assert scoped.delete_document(DOC_A) == 3
    assert scoped.entries() == {(4, DOC_B, VER_B), (5, DOC_B, VER_B)}
    assert scoped.delete_document(DOC_A) == 0


def test_deleting_a_version_removes_only_its_vectors(index: VectorIndex) -> None:
    index.put([row(1, [1, 0, 0, 0]), row(2, [0, 1, 0, 0], ver=VER_A2)])
    assert index.delete_version(VER_A) == 1
    assert index.entries() == {(2, DOC_A, VER_A2)}


def test_deleting_chunks_removes_those(scoped: VectorIndex) -> None:
    assert scoped.delete_chunks({1, 5, 99}) == 2
    assert {chunk for chunk, _, _ in scoped.entries()} == {2, 3, 4}


# --- input checks ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector",
    [
        [1, 0, 0],  # too short
        [1, 0, 0, 0, 0],  # too long
        [math.nan, 0, 0, 0],
        [math.inf, 0, 0, 0],
        [1e39, 0, 0, 0],  # beyond float32
        [True, 0, 0, 0],  # a bool is not a number here
        ["1", 0, 0, 0],
        [0, 0, 0, 0],  # no direction for a cosine
        [0.0, -0.0, 0, 0],
        "abcd",
    ],
)
def test_a_vector_it_cannot_rank_is_refused_and_nothing_is_written(
    index: VectorIndex, vector: list[float]
) -> None:
    with pytest.raises(ValueError, match="vector") as caught:
        index.put([row(1, [1, 0, 0, 0]), row(2, vector)])
    assert "abcd" not in str(caught.value)
    assert index.count() == 0
    with pytest.raises(ValueError, match="vector"):
        index.search(vector, 1)


@pytest.mark.parametrize(
    ("chunk_id", "doc", "ver"),
    [
        (0, DOC_A, VER_A),
        (-1, DOC_A, VER_A),
        (True, DOC_A, VER_A),
        ("1", DOC_A, VER_A),
        (1, "not-a-ulid", VER_A),
        (1, f"{DOC_A}\n", VER_A),
        (1, DOC_A.lower(), VER_A),
        (1, DOC_A, "Patient-Name"),
    ],
)
def test_ids_are_checked_before_anything_is_written(
    index: VectorIndex, chunk_id: int, doc: str, ver: str
) -> None:
    with pytest.raises(ValueError, match="id"):
        index.put([row(2, [1, 0, 0, 0]), ChunkVector(chunk_id, doc, ver, [1, 0, 0, 0])])
    assert index.count() == 0


def test_two_vectors_for_one_chunk_in_one_call_are_refused(index: VectorIndex) -> None:
    with pytest.raises(ValueError, match="more than one"):
        index.put([row(1, [1, 0, 0, 0]), row(1, [0, 1, 0, 0])])
    assert index.count() == 0


@pytest.mark.parametrize("k", [0, -1, vectors.MAX_K + 1, True, 1.0])
def test_k_must_be_in_range(index: VectorIndex, k: int) -> None:
    with pytest.raises(ValueError, match="k must"):
        index.search([1, 0, 0, 0], k)


def test_filters_are_checked(index: VectorIndex) -> None:
    with pytest.raises(ValueError, match="chunk id"):
        index.search([1, 0, 0, 0], 1, chunk_ids={True})
    with pytest.raises(ValueError, match="document id"):
        index.search([1, 0, 0, 0], 1, document_ids={"Patient-Name"})
    with pytest.raises(ValueError, match="document id"):
        index.delete_document("Patient-Name")
    with pytest.raises(ValueError, match="version id"):
        index.delete_version("")


@pytest.mark.parametrize(
    ("model", "dims"),
    [
        ("", DIMS),
        ("has space", DIMS),
        (f"{MODEL}\n", DIMS),
        (f" {MODEL}", DIMS),
        ("a" * 201, DIMS),
        (MODEL, 0),
        (MODEL, vectors.MAX_DIMENSIONS + 1),
        (MODEL, True),
        (MODEL, 4.0),
    ],
)
def test_a_model_id_or_dimension_count_out_of_grammar_is_refused(
    path: Path, model: str, dims: int
) -> None:
    with pytest.raises(ValueError, match=r"model id|dimension"):
        VectorIndex.open(path, model, dims)
    assert not path.exists()


# --- one model per index -----------------------------------------------------------------------


@pytest.mark.parametrize(("model", "dims"), [("other/model@1", DIMS), (MODEL, DIMS + 1)])
def test_another_model_or_dimension_count_is_refused_and_the_index_kept(
    path: Path, model: str, dims: int
) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put([row(1, [1, 0, 0, 0])])
    with pytest.raises(IndexModelMismatch) as caught:
        VectorIndex.open(path, model, dims)
    assert (caught.value.model, caught.value.dimensions) == (MODEL, DIMS)
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.count() == 1


def test_create_rebuilds_the_index_for_a_new_model(path: Path) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put([row(1, [1, 0, 0, 0])])
    with VectorIndex.create(path, "other/model@1", 2) as index:
        assert (index.model, index.dimensions, index.count()) == ("other/model@1", 2, 0)
    with pytest.raises(IndexModelMismatch):
        VectorIndex.open(path, MODEL, DIMS)


# --- damage and the file itself ----------------------------------------------------------------


def sibling_vault_db(path: Path) -> bytes:
    """A stand-in vault.db beside the index: its bytes must never change."""
    data = SQLITE_HEADER + b"synthetic irreplaceable rows" * 64
    (path.parent / "vault.db").write_bytes(data)
    return data


def write_foreign_database(path: Path, statements: list[str]) -> None:
    connection = sqlite3.connect(path)
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()


VEC = "CREATE VIRTUAL TABLE chunk_vectors USING vec0("
# Each layout an index may be found in that is not the one the module writes. Every one of them is
# rebuildable data in a bad state, never a SQL error that stops the start-up.
DAMAGE: dict[str, list[str]] = {
    "other format": ["UPDATE index_meta SET value = '2' WHERE key = 'format'"],
    "meta without the vector table": ["DROP TABLE chunk_vectors"],
    "vector table of another layout": [
        "UPDATE index_meta SET value = '3' WHERE key = 'dimensions'"
    ],
    "dimensions not a number": ["UPDATE index_meta SET value = 'four' WHERE key = 'dimensions'"],
    "dimensions with a leading zero": [
        "UPDATE index_meta SET value = '04' WHERE key = 'dimensions'"
    ],
    "a meta row missing": ["DELETE FROM index_meta WHERE key = 'model'"],
    "an extra meta row": ["INSERT INTO index_meta VALUES ('extra', 'x')"],
    "meta with a renamed column": [
        "DROP TABLE index_meta",
        "CREATE TABLE index_meta (key TEXT, wrong_column TEXT)",
    ],
    "meta missing a column": [
        "DROP TABLE index_meta",
        "CREATE TABLE index_meta (key TEXT PRIMARY KEY NOT NULL)",
        "INSERT INTO index_meta VALUES ('format'), ('model'), ('dimensions')",
    ],
    "meta with an extra column": [
        "DROP TABLE index_meta",
        "CREATE TABLE index_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL, note TEXT)",
        "INSERT INTO index_meta (key, value) VALUES ('format', '1'),"
        " ('model', 'synthetic/test-model@0123abc'), ('dimensions', '4')",
    ],
    "meta as a view": [
        "DROP TABLE index_meta",
        "CREATE VIEW index_meta AS SELECT 'format' AS key, '1' AS value"
        " UNION ALL SELECT 'model', 'synthetic/test-model@0123abc'"
        " UNION ALL SELECT 'dimensions', '4'",
    ],
    "meta with a value that is not text": [
        "UPDATE index_meta SET value = x'00' WHERE key = 'model'"
    ],
    "vector table without filter columns": [
        "DROP TABLE chunk_vectors",
        f"{VEC}chunk_id integer primary key, embedding float[4] distance_metric=cosine)",
    ],
    "vector table with another distance": [
        "DROP TABLE chunk_vectors",
        f"{VEC}chunk_id integer primary key, embedding float[4],"
        " document_id text, version_id text)",
    ],
    "another virtual table in its place": [
        "DROP TABLE chunk_vectors",
        "CREATE VIRTUAL TABLE chunk_vectors USING fts5(document_id, version_id)",
    ],
    "a plain table in its place": [
        "DROP TABLE chunk_vectors",
        "CREATE TABLE chunk_vectors (chunk_id INTEGER, document_id TEXT, version_id TEXT)",
    ],
    "a view in its place": [
        "DROP TABLE chunk_vectors",
        "CREATE VIEW chunk_vectors AS SELECT 1 AS chunk_id",
    ],
    "a shadow table changed": ["ALTER TABLE chunk_vectors_rowids ADD COLUMN extra TEXT"],
    "an extra table": ["CREATE TABLE notes (body TEXT)"],
    "an extra trigger": [
        "CREATE TRIGGER meta_changed AFTER UPDATE ON index_meta BEGIN SELECT 1; END"
    ],
    "an extra index": ["CREATE INDEX meta_value ON index_meta (value)"],
}


def alter(path: Path, statements: list[str]) -> None:
    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    connection.load_extension(sqlite_vec.loadable_path())
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()


@pytest.mark.parametrize("damage", ["garbage", "truncated", "unrelated tables", *DAMAGE])
def test_a_damaged_index_is_made_anew_without_touching_vault_db(path: Path, damage: str) -> None:
    vault_db = sibling_vault_db(path)
    if damage == "garbage":
        path.write_bytes(b"synthetic damage " * 512)
    elif damage == "truncated":
        with VectorIndex.open(path, MODEL, DIMS) as index:
            index.put([row(chunk, [1, chunk, 0, 0]) for chunk in range(1, 300)])
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
    elif damage == "unrelated tables":
        write_foreign_database(path, ["CREATE TABLE notes (body TEXT)"])
    else:
        with VectorIndex.open(path, MODEL, DIMS) as index:
            index.put([row(1, [1, 0, 0, 0])])
        alter(path, DAMAGE[damage])
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.count() == 0
        index.put([row(1, [1, 0, 0, 0])])
        assert index.count() == 1
    assert (path.parent / "vault.db").read_bytes() == vault_db


@pytest.mark.parametrize("damage", list(DAMAGE))
def test_the_sweep_and_the_delete_rebuild_an_incompatible_layout(path: Path, damage: str) -> None:
    # The start-up sweep and the permanent delete open the index without a model; an incompatible
    # layout must be removed there too, never raise a SQL error that stops the server.
    for step in ("sweep", "delete"):
        with VectorIndex.open(path, MODEL, DIMS) as index:
            index.put([row(1, [1, 0, 0, 0])])
        alter(path, DAMAGE[damage])
        if step == "sweep":
            assert vectors.sweep(path, set) == vectors.Swept(removed=0, rebuilt=True)
        else:
            assert vectors.remove_document(path, DOC_A) == 0
        assert not path.exists()


def test_the_layout_this_module_writes_is_not_damage(path: Path) -> None:
    # The control for the cases above: the comparison accepts exactly what open() writes.
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put([row(1, [1, 0, 0, 0])])
    alter(path, [])
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.count() == 1


def test_a_missing_index_is_made_owner_only(path: Path) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.count() == 0
    assert path.read_bytes().startswith(SQLITE_HEADER)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_empty_file_becomes_an_index(path: Path) -> None:
    path.write_bytes(b"")
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.count() == 0


def test_damaged_side_files_go_with_the_index(path: Path) -> None:
    path.write_bytes(b"synthetic damage " * 512)
    journal = Path(f"{path}-journal")
    journal.write_bytes(b"")
    with VectorIndex.open(path, MODEL, DIMS):
        pass
    assert not journal.exists()


def test_a_busy_index_is_not_damage(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put([row(1, [1, 0, 0, 0])])
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("UPDATE index_meta SET value = value")
    monkeypatch.setattr(vectors, "BUSY_TIMEOUT_MS", 0)
    try:
        with pytest.raises(sqlite3.OperationalError):
            VectorIndex.open(path, MODEL, DIMS)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.count() == 1


def test_an_extension_that_does_not_load_is_a_typed_error_and_nothing_is_removed(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put([row(1, [1, 0, 0, 0])])
    monkeypatch.setattr(sqlite_vec, "loadable_path", lambda: str(path.parent / "nothing"))
    with pytest.raises(VectorIndexUnavailable):
        VectorIndex.open(path, MODEL, DIMS)
    monkeypatch.undo()
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.count() == 1


def test_the_extension_loads_into_this_python() -> None:
    # The CI jobs on Linux and Windows run this: the wheel's vec0 loads into their sqlite3.
    connection = sqlite3.connect(":memory:")
    connection.enable_load_extension(True)
    connection.load_extension(sqlite_vec.loadable_path())
    assert connection.execute("SELECT vec_version()").fetchone()[0].startswith("v0.")
    connection.close()


# --- links -------------------------------------------------------------------------------------


def outside(tmp_path: Path) -> tuple[Path, bytes]:
    folder = tmp_path / "outside"
    folder.mkdir()
    target = folder / "elsewhere.db"
    data = b"synthetic outside file"
    target.write_bytes(data)
    return target, data


@pytest.mark.parametrize("name", ["index.db", "index.db-wal", "index.db-shm", "index.db-journal"])
def test_a_link_where_the_index_or_a_side_file_belongs_is_refused(
    tmp_path: Path, name: str
) -> None:
    target, data = outside(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    link(target, vault / name)
    path = vault / "index.db"
    with pytest.raises(IndexPathError):
        VectorIndex.open(path, MODEL, DIMS)
    with pytest.raises(IndexPathError):
        VectorIndex.create(path, MODEL, DIMS)
    with pytest.raises(IndexPathError):
        vectors.remove_document(path, DOC_A)
    with pytest.raises(IndexPathError):
        vectors.sweep(path, set)
    assert target.read_bytes() == data
    assert (vault / name).is_symlink()


def test_a_dangling_link_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    link(tmp_path / "missing.db", path)
    with pytest.raises(IndexPathError):
        VectorIndex.open(path, MODEL, DIMS)
    assert not (tmp_path / "missing.db").exists()


def test_a_hard_link_is_refused(tmp_path: Path) -> None:
    target, data = outside(tmp_path)
    path = tmp_path / "index.db"
    try:
        os.link(target, path)
    except OSError:
        pytest.skip("this file system cannot make hard links")
    with pytest.raises(IndexPathError):
        VectorIndex.open(path, MODEL, DIMS)
    assert target.read_bytes() == data


def test_a_folder_where_the_index_belongs_is_refused(path: Path) -> None:
    path.mkdir()
    with pytest.raises(IndexPathError):
        VectorIndex.open(path, MODEL, DIMS)
    assert path.is_dir()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_a_junction_where_the_index_belongs_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "outside"
    folder.mkdir()
    path = tmp_path / "index.db"
    winapi = importlib.import_module("_winapi")  # typed only on Windows
    winapi.CreateJunction(str(folder), str(path))
    with pytest.raises(IndexPathError):
        VectorIndex.open(path, MODEL, DIMS)
    assert list(folder.iterdir()) == []


# --- delete and sweep without a model ----------------------------------------------------------


def test_removing_a_document_needs_no_index(path: Path) -> None:
    assert vectors.remove_document(path, DOC_A) == 0
    assert not path.exists()


def test_removing_a_document_works_whatever_model_built_the_index(path: Path) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put([row(1, [1, 0, 0, 0]), row(2, [0, 1, 0, 0], DOC_B, VER_B)])
    assert vectors.remove_document(path, DOC_A) == 1
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.entries() == {(2, DOC_B, VER_B)}


def test_removing_a_document_from_a_damaged_index_removes_the_file(path: Path) -> None:
    path.write_bytes(b"synthetic damage " * 512)
    assert vectors.remove_document(path, DOC_A) == 0
    assert not path.exists()


def test_the_sweep_removes_vectors_of_chunks_that_are_gone(path: Path) -> None:
    with VectorIndex.open(path, MODEL, DIMS) as index:
        index.put(
            [
                row(1, [1, 0, 0, 0]),
                row(2, [0, 1, 0, 0]),
                row(3, [0, 0, 1, 0], DOC_B, VER_B),
            ]
        )
    # Chunk 2 is gone; chunk 3 is recorded under another document, which is not its vector.
    live = {(1, DOC_A, VER_A), (3, DOC_A, VER_A), (4, DOC_A, VER_A)}
    assert vectors.sweep(path, lambda: live) == vectors.Swept(removed=2, rebuilt=False)
    with VectorIndex.open(path, MODEL, DIMS) as index:
        assert index.entries() == {(1, DOC_A, VER_A)}


def test_the_sweep_reads_nothing_when_there_is_no_index(path: Path) -> None:
    def unread() -> set[tuple[int, str, str]]:
        raise AssertionError

    assert vectors.sweep(path, unread) == vectors.Swept(removed=0, rebuilt=False)
    assert not path.exists()


def test_the_sweep_removes_a_damaged_index(path: Path) -> None:
    vault_db = sibling_vault_db(path)
    path.write_bytes(b"synthetic damage " * 512)
    assert vectors.sweep(path, set) == vectors.Swept(removed=0, rebuilt=True)
    assert not path.exists()
    assert (path.parent / "vault.db").read_bytes() == vault_db
