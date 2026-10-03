"""Spike 1: does sqlite-vec load, and how fast is exact search at 10k to 100k vectors?

Decides whether ADR 0004's choice of sqlite-vec for ``index.db`` holds on the Windows PC. The
vectors are random numbers made here (no document is involved). The store is a small interface so
the unit tests can run the whole spike against a plain-Python store.
"""

from __future__ import annotations

import array
import os
import random
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from itertools import islice, pairwise
from pathlib import Path
from typing import Protocol

from vault_spikes.errors import OwnRuntimeError, OwnValueError
from vault_spikes.metrics import mean, peak_memory_mib, per_second, percentile
from vault_spikes.report import Environment, Kind, Report, RowShape, collect_environment

TITLE = "sqlite-vec on this PC"
QUESTION = (
    "Does the sqlite-vec extension load into this Python's sqlite3, and what are the insert rate, "
    "database size and exact-search latency at 10,000 and 100,000 vectors? Feeds ADR 0004."
)
METHOD = (
    "`python -m vault_spikes vec`. Random unit-free float32 vectors from a seeded generator are "
    "inserted into a `vec0` table; each size is queried with random vectors for the 10 nearest "
    "neighbours (exact, brute force). Latency is wall-clock time per query. Correctness compares "
    "the extension's answers with a plain-Python search on the smallest size: the same ten "
    "neighbours in the same order, because the application uses the ranking (two neighbours whose "
    "distances are equal within float32 rounding may swap). No document text is involved."
)
PACKAGES = ("sqlite-vec",)
# What a report of this spike may contain (checked when a report is read back for a merge).
METRICS: dict[str, Kind] = {
    "can_load_extensions": Kind.FLAG,
    "vec_loads": Kind.FLAG,
    "vectors": Kind.COUNT,
    "same_answer_as_python": Kind.RATIO,
    "dim": Kind.COUNT,
    "queries_timed": Kind.COUNT,
    "insert_vectors_per_second": Kind.AMOUNT,
    "database_mib": Kind.AMOUNT,
    "query_ms_p50": Kind.AMOUNT,
    "query_ms_p95": Kind.AMOUNT,
    "peak_memory_mib": Kind.AMOUNT,
}
ROWS = (
    RowShape(
        re.compile(r"extension-loading"),
        frozenset({"can_load_extensions", "vec_loads"}),
        # An extension cannot load into a Python that cannot load extensions.
        consistent=lambda row: bool(row["can_load_extensions"]) or not row["vec_loads"],
    ),
    RowShape(
        re.compile(r"exact-search-check"),
        frozenset({"vectors", "same_answer_as_python"}),
        consistent=lambda row: row["vectors"] > 0,
    ),
    RowShape(
        re.compile(r"vectors-\d{1,9}"),
        frozenset(
            {
                "dim",
                "queries_timed",
                "insert_vectors_per_second",
                "database_mib",
                "query_ms_p50",
                "query_ms_p95",
            }
        ),
        frozenset({"peak_memory_mib"}),
        consistent=lambda row: row["dim"] > 0 and row["queries_timed"] > 0,
    ),
)
"""The metrics of each kind of row. A report read from a file must give every ``required`` one and
nothing outside ``required`` and ``optional``, so a row cannot lose its context or borrow another
row's figures."""
CANDIDATES = re.compile("|".join(f"(?:{shape.candidate.pattern})" for shape in ROWS))


class VectorStore(Protocol):
    """What the spike needs from a vector index."""

    def insert(self, rows: Iterable[tuple[int, bytes]]) -> None: ...

    def nearest(self, query: bytes, k: int) -> list[int]: ...

    def size_mib(self) -> float: ...

    def close(self) -> None: ...


StoreFactory = Callable[[int], VectorStore]


def random_vectors(count: int, dim: int, seed: int) -> Iterator[bytes]:
    """``count`` random float32 vectors, as the bytes the extension stores."""
    rng = random.Random(seed)  # noqa: S311 - not security related
    for _ in range(count):
        yield array.array("f", (rng.random() for _ in range(dim))).tobytes()


def l2_distance(a: bytes, b: bytes) -> float:
    """Squared Euclidean distance between two float32 vectors."""
    return sum((x - y) ** 2 for x, y in zip(array.array("f", a), array.array("f", b), strict=True))


def l2_nearest(vectors: list[bytes], query: bytes, k: int) -> list[int]:
    """Plain-Python exact search: the ids (1-based) of the ``k`` vectors closest to ``query``,
    nearest first."""
    order = sorted(range(len(vectors)), key=lambda i: l2_distance(query, vectors[i]))
    return [i + 1 for i in order[:k]]


TIE_TOLERANCE = 1e-4
"""Two distances this close (relative) are a tie: sqlite-vec works in float32 and this script in
float64, so they may order neighbours that are a rounding error apart either way."""


def same_ranking(found: list[int], vectors: list[bytes], query: bytes, k: int) -> bool:
    """Does ``found`` give the exact nearest neighbours *in order*? The application consumes a
    ranked list, so the right ten ids from farthest to nearest are a wrong answer. Place by place,
    the id must be the exact search's, or one at a distance equal to it within ``TIE_TOLERANCE``
    (a tie, which either order satisfies); an id outside the data, or a repeated one, is wrong."""
    expected = l2_nearest(vectors, query, k)
    if len(found) != len(expected) or len(set(found)) != len(found):
        return False
    for got, want in zip(found, expected, strict=True):
        if not 1 <= got <= len(vectors):
            return False
        if got != want:
            near = l2_distance(query, vectors[got - 1])
            exact = l2_distance(query, vectors[want - 1])
            if abs(near - exact) > TIE_TOLERANCE * max(near, exact, 1e-12):
                return False
    return True


class PythonStore:
    """A store that keeps vectors in a list. Used by the tests, and as the reference answer."""

    def __init__(self) -> None:
        self.vectors: list[bytes] = []

    def insert(self, rows: Iterable[tuple[int, bytes]]) -> None:
        for _id, blob in rows:
            self.vectors.append(blob)

    def nearest(self, query: bytes, k: int) -> list[int]:
        return l2_nearest(self.vectors, query, k)

    def size_mib(self) -> float:
        return sum(len(v) for v in self.vectors) / 1024**2

    def close(self) -> None:
        self.vectors.clear()


class SqliteVecStore:
    """The real thing: a ``vec0`` table in its own temporary database file.

    Every store gets a database file of its own and removes it on close, so one run can build
    several stores in the same folder (the correctness check and each measured size).
    """

    def __init__(self, dim: int, directory: Path) -> None:
        handle, name = tempfile.mkstemp(prefix="spike-vec-", suffix=".db", dir=directory)
        os.close(handle)
        self.path = Path(name)
        self.db = sqlite3.connect(self.path)
        self._load_extension()
        self._create_table(dim)

    def _load_extension(self) -> None:
        import sqlite_vec

        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)

    def _create_table(self, dim: int) -> None:
        self.db.execute(f"CREATE VIRTUAL TABLE v USING vec0(embedding float[{dim}])")

    def insert(self, rows: Iterable[tuple[int, bytes]]) -> None:
        with self.db:
            self.db.executemany("INSERT INTO v(rowid, embedding) VALUES (?, ?)", rows)

    def nearest(self, query: bytes, k: int) -> list[int]:
        cursor = self.db.execute(
            "SELECT rowid FROM v WHERE embedding MATCH ? AND k = ? ORDER BY distance", (query, k)
        )
        return [int(row[0]) for row in cursor]

    def size_mib(self) -> float:
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return self.path.stat().st_size / 1024**2

    def close(self) -> None:
        self.db.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)


def probe_extension_loading() -> dict[str, bool]:
    """Can this Python's sqlite3 load extensions, and does sqlite-vec load? (A Python build can
    ship without ``enable_load_extension``, which would end the sqlite-vec plan on this PC.)"""
    facts = {"can_load_extensions": hasattr(sqlite3.Connection, "enable_load_extension")}
    facts["vec_loads"] = False
    if facts["can_load_extensions"]:
        try:
            with tempfile.TemporaryDirectory() as folder:
                SqliteVecStore(4, Path(folder)).close()
            facts["vec_loads"] = True
        except Exception:
            facts["vec_loads"] = False
    return facts


def check_sizes(sizes: Sequence[int]) -> None:
    """Refuse sizes that are not listed from the smallest to the largest, each once.

    ``peak_memory_mib`` is the process's high-water mark and only grows, so a larger size run
    first would put its peak in every later row (``vectors-10000`` would report the memory of a
    100,000-vector store). In ascending order each row's peak is its own: nothing before it was
    larger. A size listed twice would also give two rows the same name.
    """
    if any(smaller >= larger for smaller, larger in pairwise(sizes)):
        msg = "sizes must be listed from the smallest to the largest, each once"
        raise OwnValueError(msg)


def run_vec_spike(
    make_store: StoreFactory,
    *,
    loading: dict[str, bool] | None = None,
    sizes: tuple[int, ...] = (10_000, 100_000),
    dim: int = 1024,
    queries: int = 30,
    k: int = 10,
    check_size: int = 500,
    seed: int = 7,
    environment: Environment | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Report:
    """Measure each size in ``sizes`` and the correctness of the store at ``check_size``.

    ``loading`` is the result of :func:`probe_extension_loading`. If the extension does not load,
    that is the whole result, and nothing else is measured.
    """
    check_sizes(sizes)
    report = Report(
        "vec",
        TITLE,
        QUESTION,
        METHOD,
        environment or collect_environment(PACKAGES),
    )
    if loading is not None:
        report.add("extension-loading", **loading)
        if not loading.get("vec_loads", False):
            return report

    # Correctness first, on a small set, against the plain-Python answer.
    vectors = list(random_vectors(check_size, dim, seed))
    store = make_store(dim)
    try:
        store.insert(enumerate(vectors, start=1))
        probes = list(random_vectors(5, dim, seed + 1))
        matches = [same_ranking(store.nearest(p, k), vectors, p, k) for p in probes]
    finally:
        store.close()
    report.add(
        "exact-search-check",
        vectors=check_size,
        same_answer_as_python=mean([1.0 if m else 0.0 for m in matches]),
    )

    for size in sizes:
        store = make_store(dim)
        try:
            insert_seconds = 0.0
            number = 0
            generated = random_vectors(size, dim, seed)
            while True:
                # Time only the inserts, not the generation of the random numbers.
                batch = [(number + i, blob) for i, blob in enumerate(islice(generated, 1000), 1)]
                if not batch:
                    break
                number += len(batch)
                begin = clock()
                store.insert(batch)
                insert_seconds += clock() - begin
            timings = []
            for query in random_vectors(queries, dim, seed + 2):
                begin = clock()
                found = store.nearest(query, k)
                timings.append((clock() - begin) * 1000)
                if len(found) != min(k, size):
                    msg = "the store returned the wrong number of neighbours"
                    raise OwnRuntimeError(msg)
            metrics: dict[str, int | float] = {
                "dim": dim,
                "queries_timed": queries,
                "insert_vectors_per_second": per_second(size, insert_seconds),
                "database_mib": store.size_mib(),
                "query_ms_p50": percentile(timings, 0.5),
                "query_ms_p95": percentile(timings, 0.95),
            }
            if (peak := peak_memory_mib()) is not None:
                metrics["peak_memory_mib"] = peak
            report.add(f"vectors-{size}", **metrics)
        finally:
            store.close()
    return report
