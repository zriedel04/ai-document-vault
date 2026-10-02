"""Spike 1: does sqlite-vec load, and how fast is exact search at 10k to 100k vectors?

Decides whether ADR 0004's choice of sqlite-vec for ``index.db`` holds on the Windows PC. The
vectors are random numbers made here (no document is involved). The store is a small interface so
the unit tests can run the whole spike against a plain-Python store.
"""

from __future__ import annotations

import array
import os
import random
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from itertools import islice
from pathlib import Path
from typing import Protocol

from vault_spikes.metrics import mean, peak_memory_mib, per_second, percentile
from vault_spikes.report import Environment, Report, collect_environment

QUESTION = (
    "Does the sqlite-vec extension load into this Python's sqlite3, and what are the insert rate, "
    "database size and exact-search latency at 10,000 and 100,000 vectors? Feeds ADR 0004."
)
METHOD = (
    "`python -m vault_spikes vec`. Random unit-free float32 vectors from a seeded generator are "
    "inserted into a `vec0` table; each size is queried with random vectors for the 10 nearest "
    "neighbours (exact, brute force). Latency is wall-clock time per query. Correctness compares "
    "the extension's answers with a plain-Python search on the smallest size. No document text is "
    "involved."
)
PACKAGES = ("sqlite-vec",)


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


def l2_nearest(vectors: list[bytes], query: bytes, k: int) -> list[int]:
    """Plain-Python exact search: the ids (1-based) of the ``k`` vectors closest to ``query``."""
    q = array.array("f", query)

    def distance(blob: bytes) -> float:
        v = array.array("f", blob)
        return sum((a - b) ** 2 for a, b in zip(q, v, strict=True))

    order = sorted(range(len(vectors)), key=lambda i: distance(vectors[i]))
    return [i + 1 for i in order[:k]]


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
    report = Report(
        "vec",
        "sqlite-vec on this PC",
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
        matches = [set(store.nearest(p, k)) == set(l2_nearest(vectors, p, k)) for p in probes]
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
                    raise RuntimeError(msg)
            metrics: dict[str, int | float] = {
                "dim": dim,
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
