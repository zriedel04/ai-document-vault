"""The chunk stage: stored pages become chunks and index rows, and the document is searchable."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, delete, select, update

import vault.db.engine
from tests.conftest import LOCAL
from tests.unit.test_chunks import assert_index_in_step, course_pages
from tests.unit.test_documents_api import Library
from tests.unit.test_documents_schema import OTHER_SHA
from vault.db import ids
from vault.db.pages import PageHeading, PageText, replace_pages
from vault.db.schema import TRASHED, chunks, document_versions, documents, jobs
from vault.ingest import chunk_stage
from vault.ingest.chunk_stage import (
    CHUNK_STAGE,
    Outcome,
    PagesChangedError,
    PagesMissingError,
    chunk_document,
)
from vault.ingest.chunking import structural_v1
from vault.jobs.queue import Completion, Job, JobLostError, JobQueue, Recovery
from vault.jobs.worker import WorkerPool

CANARY = "canary-chunk-stage-xyzzy"


def imported(library: Library, *, extracted: bool = True) -> tuple[str, str, str]:
    """A document imported as an upload, with the course pages stored for its version unless
    ``extracted`` is false: its id, its version's id and the SHA-256 of its bytes."""
    document_id = library.add("course.md", f"# Course {CANARY} {ids.new_ulid()}\n")
    with library.engine.connect() as connection:
        version_id, sha256 = connection.execute(
            select(document_versions.c.id, document_versions.c.blob_sha256).where(
                document_versions.c.document_id == document_id
            )
        ).one()
    if extracted:
        replace_pages(library.engine, version_id, course_pages(), "text-1", "utf-8")
    return document_id, version_id, sha256


def state(engine: Engine, document_id: str) -> tuple[str, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            select(documents.c.state, documents.c.state_before_trash).where(
                documents.c.id == document_id
            )
        ).one()
    return row.state, row.state_before_trash


def set_state(engine: Engine, document_id: str, value: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(documents).where(documents.c.id == document_id).values(state=value)
        )


def stored_chunks(engine: Engine, version_id: str) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                select(
                    chunks.c.chunk_index,
                    chunks.c.page_start,
                    chunks.c.char_start,
                    chunks.c.char_end,
                    chunks.c.text,
                    chunks.c.section_path,
                    chunks.c.chunker_version,
                )
                .where(chunks.c.version_id == version_id)
                .order_by(chunks.c.chunk_index)
            )
        ]


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


@pytest.fixture
def engine(app: FastAPI) -> Engine:
    result: Engine = app.state.engine
    return result


def test_the_pages_become_the_chunkers_chunks_and_the_document_is_searchable(
    library: Library, engine: Engine
) -> None:
    document_id, version_id, sha256 = imported(library)
    assert state(engine, document_id) == ("processing", None)
    assert chunk_document(engine, document_id, sha256) is Outcome.STORED
    expected = structural_v1.chunk_pages(course_pages())
    rows = stored_chunks(engine, version_id)
    assert [(row[0], row[1], row[2], row[3], row[4]) for row in rows] == [
        (chunk.index, chunk.page_start, chunk.char_start, chunk.char_end, chunk.text)
        for chunk in expected
    ]
    assert {row[6] for row in rows} == {structural_v1.CHUNKER_VERSION}
    assert state(engine, document_id) == ("searchable", None)
    assert_index_in_step(engine)


def test_running_it_again_replaces_the_chunks_with_the_same_ones(
    library: Library, engine: Engine
) -> None:
    document_id, version_id, sha256 = imported(library)
    chunk_document(engine, document_id, sha256)
    first = stored_chunks(engine, version_id)
    assert chunk_document(engine, document_id, sha256) is Outcome.STORED
    assert stored_chunks(engine, version_id) == first
    assert_index_in_step(engine)


@pytest.mark.parametrize("before", ["processing", "failed", "ready", "searchable"])
def test_every_state_in_use_becomes_searchable(
    library: Library, engine: Engine, before: str
) -> None:
    document_id, _, sha256 = imported(library)
    set_state(engine, document_id, before)
    chunk_document(engine, document_id, sha256)
    assert state(engine, document_id) == ("searchable", None)


def test_a_document_in_the_trash_stays_there_and_comes_back_searchable(
    library: Library, engine: Engine
) -> None:
    document_id, version_id, sha256 = imported(library)
    library.trash(document_id)
    assert state(engine, document_id) == (TRASHED, "processing")
    assert chunk_document(engine, document_id, sha256) is Outcome.STORED
    assert state(engine, document_id) == (TRASHED, "searchable")
    assert stored_chunks(engine, version_id)
    from vault.db.documents import restore

    assert restore(engine, document_id)
    assert state(engine, document_id) == ("searchable", None)


def test_pages_with_no_visible_text_leave_no_chunks_and_a_searchable_document(
    library: Library, engine: Engine
) -> None:
    document_id, version_id, sha256 = imported(library, extracted=False)
    replace_pages(engine, version_id, [PageText(1, " \n\n "), PageText(2, "")], "text-1", "utf-8")
    assert chunk_document(engine, document_id, sha256) is Outcome.STORED
    assert stored_chunks(engine, version_id) == []
    assert state(engine, document_id) == ("searchable", None)


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(lambda document_id, sha256: (document_id, OTHER_SHA), id="other bytes"),
        pytest.param(lambda document_id, sha256: ("0" * 26, sha256), id="unknown document"),
        pytest.param(lambda document_id, sha256: ("not-an-id", sha256), id="not an id"),
        pytest.param(lambda document_id, sha256: (document_id.lower(), sha256), id="lower case"),
    ],
)
def test_a_job_for_bytes_the_document_does_not_hold_changes_nothing(
    library: Library, engine: Engine, target: Callable[[str, str], tuple[str, str]]
) -> None:
    document_id, version_id, sha256 = imported(library)
    assert chunk_document(engine, *target(document_id, sha256)) is Outcome.STALE
    assert stored_chunks(engine, version_id) == []
    assert state(engine, document_id) == ("processing", None)


def test_a_version_with_no_pages_is_retried_later(library: Library, engine: Engine) -> None:
    document_id, version_id, sha256 = imported(library, extracted=False)
    with pytest.raises(PagesMissingError) as raised:
        chunk_document(engine, document_id, sha256)
    assert CANARY not in str(raised.value)
    assert stored_chunks(engine, version_id) == []
    assert state(engine, document_id) == ("processing", None)


def chunking_with(
    monkeypatch: pytest.MonkeyPatch, meanwhile: Callable[[], None]
) -> list[Sequence[PageText]]:
    """Make the chunker run ``meanwhile`` after it has chunked, as another writer would, between
    the stage's read of the pages and its write."""
    real = structural_v1.chunk_pages
    seen: list[Sequence[PageText]] = []

    def chunk_then_interfere(pages_in: Sequence[PageText]) -> Any:
        result = real(pages_in)
        seen.append(pages_in)
        meanwhile()
        return result

    monkeypatch.setattr(structural_v1, "chunk_pages", chunk_then_interfere)
    return seen


def renamed_pages() -> list[PageText]:
    """The course pages with the same text and other headings: every chunk still matches its
    page's text, so only comparing the pages shows that section paths would come from headings
    that are gone."""
    return [
        PageText(
            page.number, page.text, (PageHeading(1, "Renamed", 0, 18),) if page.headings else ()
        )
        for page in course_pages()
    ]


def test_pages_whose_headings_change_while_they_are_chunked_are_chunked_again(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, version_id, sha256 = imported(library)
    changes = [renamed_pages()]

    def change_once() -> None:
        if changes:
            replace_pages(engine, version_id, changes.pop(), "text-1", "utf-8")

    seen = chunking_with(monkeypatch, change_once)
    assert chunk_document(engine, document_id, sha256) is Outcome.STORED
    assert len(seen) == 2  # the first chunks were made from headings that were gone
    assert any('"Renamed"' in row[5] for row in stored_chunks(engine, version_id))
    assert state(engine, document_id) == ("searchable", None)


def test_pages_that_keep_changing_fail_the_attempt_and_store_nothing(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, version_id, sha256 = imported(library)
    versions = [course_pages(), renamed_pages()]

    def change_every_time() -> None:
        versions.reverse()
        replace_pages(engine, version_id, versions[0], "text-1", "utf-8")

    seen = chunking_with(monkeypatch, change_every_time)
    with pytest.raises(PagesChangedError) as raised:
        chunk_document(engine, document_id, sha256)
    assert CANARY not in str(raised.value)
    assert len(seen) == chunk_stage.MAX_ROUNDS
    assert stored_chunks(engine, version_id) == []
    assert state(engine, document_id) == ("processing", None)
    # With a job the failed attempt is recorded in the transaction that found it.
    queue, job = last_attempt(engine, document_id, sha256)
    assert chunk_stage.handler(engine, queue)(job) is Completion.ATTEMPT_FAILED
    assert job_row(engine, job.id).last_error == "PagesChangedError"
    assert state(engine, document_id) == ("failed", None)


def test_a_document_deleted_while_its_pages_are_chunked_is_left_alone(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, _, sha256 = imported(library)

    def delete_it() -> None:
        with engine.begin() as connection:
            connection.execute(delete(documents).where(documents.c.id == document_id))

    chunking_with(monkeypatch, delete_it)
    assert chunk_document(engine, document_id, sha256) is Outcome.STALE
    with engine.connect() as connection:
        assert connection.execute(select(chunks.c.id)).first() is None


def test_a_document_whose_version_changes_while_it_is_chunked_is_left_alone(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The current version moves to other bytes (a later import of an edited file would do this).
    document_id, version_id, sha256 = imported(library)
    other_id, other_version, _ = imported(library)
    swap_sha = stored_sha(engine, other_version)

    def move_it() -> None:
        with engine.begin() as connection:
            connection.execute(
                update(document_versions)
                .where(document_versions.c.id == version_id)
                .values(blob_sha256=swap_sha)
            )

    chunking_with(monkeypatch, move_it)
    assert chunk_document(engine, document_id, sha256) is Outcome.STALE
    assert stored_chunks(engine, version_id) == []
    assert state(engine, document_id) == ("processing", None)
    assert other_id != document_id


def stored_sha(engine: Engine, version_id: str) -> str:
    with engine.connect() as connection:
        value: str = connection.execute(
            select(document_versions.c.blob_sha256).where(document_versions.c.id == version_id)
        ).scalar_one()
    return value


def wait_for(condition: Callable[[], bool], seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def job_row(engine: Engine, job_id: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(select(jobs).where(jobs.c.id == job_id)).one()


def test_the_worker_pool_runs_chunk_jobs_and_records_a_missing_page_by_class(
    library: Library, engine: Engine
) -> None:
    done_id, _, done_sha = imported(library)
    waiting_id, _, waiting_sha = imported(library, extracted=False)
    queue = JobQueue(engine)
    done_job = queue.enqueue(done_id, CHUNK_STAGE, done_sha)
    waiting_job = queue.enqueue(waiting_id, CHUNK_STAGE, waiting_sha)
    pool = WorkerPool(
        queue,
        handlers={CHUNK_STAGE: chunk_stage.handler(engine, queue)},
        limits={CHUNK_STAGE: 1},
        idle_seconds=0.05,
    )
    pool.start()
    try:
        wait_for(lambda: job_row(engine, done_job).status == "done")
        wait_for(lambda: job_row(engine, waiting_job).attempts >= 1)
    finally:
        pool.stop(10)
    assert state(engine, done_id) == ("searchable", None)
    assert state(engine, waiting_id) == ("processing", None)
    assert job_row(engine, waiting_job).last_error == "PagesMissingError"


def test_the_app_runs_chunk_jobs(app: FastAPI, library: Library, engine: Engine) -> None:
    document_id, _, sha256 = imported(library)
    job_id = JobQueue(engine).enqueue(document_id, CHUNK_STAGE, sha256)
    with TestClient(app, base_url=LOCAL):
        app.state.workers.wake()
        wait_for(lambda: job_row(engine, job_id).status == "done")
    assert state(engine, document_id) == ("searchable", None)


# --- the job ends with the result -----------------------------------------------------------

WORKER = ids.new_ulid()


class CrashingQueue(JobQueue):
    """A queue whose own ``complete`` fails as a process killed before reporting would: only a
    completion inside the handler's transaction reaches the database."""

    def complete(
        self, job_id: str, worker_id: str, *, connection: Connection | None = None
    ) -> None:
        if connection is None:
            raise OSError
        super().complete(job_id, worker_id, connection=connection)


def last_attempt(engine: Engine, document_id: str, sha256: str) -> tuple[JobQueue, Job]:
    """The document's chunk job, claimed on its one and only attempt."""
    queue = JobQueue(engine)
    queue.enqueue(document_id, CHUNK_STAGE, sha256, max_attempts=1)
    job = queue.claim(WORKER, [CHUNK_STAGE])
    assert job is not None
    assert job.attempts == job.max_attempts
    return queue, job


def test_a_crash_after_the_last_attempt_stored_its_chunks_leaves_the_document_searchable(
    library: Library, engine: Engine
) -> None:
    document_id, version_id, sha256 = imported(library)
    queue, job = last_attempt(engine, document_id, sha256)
    # The pool runs the handler; the process ends before the pool could report anything.
    assert chunk_stage.handler(engine, queue)(job) is Completion.RECORDED
    assert JobQueue(engine).recover() == Recovery(requeued=0, failed=0)  # the next start-up
    assert job_row(engine, job.id).status == "done"
    assert state(engine, document_id) == ("searchable", None)
    assert stored_chunks(engine, version_id) != []


def test_through_the_pool_a_lost_report_cannot_fail_a_stored_last_attempt(
    library: Library, engine: Engine
) -> None:
    document_id, _, sha256 = imported(library)
    queue = CrashingQueue(engine)
    job_id = queue.enqueue(document_id, CHUNK_STAGE, sha256, max_attempts=1)
    pool = WorkerPool(
        queue,
        handlers={CHUNK_STAGE: chunk_stage.handler(engine, queue)},
        limits={CHUNK_STAGE: 1},
        idle_seconds=0.05,
    )
    pool.start()
    try:
        wait_for(lambda: job_row(engine, job_id).status != "queued")
        wait_for(lambda: state(engine, document_id)[0] != "processing")
    finally:
        pool.stop(10)
    assert JobQueue(engine).recover() == Recovery(requeued=0, failed=0)
    assert job_row(engine, job_id).status == "done"
    assert state(engine, document_id) == ("searchable", None)


def test_a_worker_that_lost_its_chunk_job_stores_nothing(library: Library, engine: Engine) -> None:
    document_id, version_id, sha256 = imported(library)
    queue = JobQueue(engine)
    queue.enqueue(document_id, CHUNK_STAGE, sha256)
    job = queue.claim(WORKER, [CHUNK_STAGE])
    assert job is not None
    queue.recover()  # taken back while the worker ran it
    with pytest.raises(JobLostError):
        chunk_stage.handler(engine, queue)(job)
    assert stored_chunks(engine, version_id) == []
    assert state(engine, document_id) == ("processing", None)


def test_a_chunk_job_with_nothing_to_store_is_completed_by_its_handler(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, version_id, sha256 = imported(library)
    queue, job = last_attempt(engine, document_id, OTHER_SHA)  # bytes it does not hold
    assert chunk_stage.handler(engine, queue)(job) is Completion.RECORDED
    assert job_row(engine, job.id).status == "done"
    queue, job = last_attempt(engine, document_id, sha256)  # an id that names no document
    assert chunk_stage.handler(engine, queue)(replace(job, document_id="not-an-id"))
    assert job_row(engine, job.id).status == "done"
    # Found stale under the write lock: completed in that transaction.
    other_id, other_version, _ = imported(library)
    queue, job = last_attempt(engine, document_id, sha256)

    def move_it() -> None:
        with engine.begin() as connection:
            connection.execute(
                update(document_versions)
                .where(document_versions.c.id == version_id)
                .values(blob_sha256=stored_sha(engine, other_version))
            )

    chunking_with(monkeypatch, move_it)
    assert chunk_stage.handler(engine, queue)(job) is Completion.RECORDED
    assert job_row(engine, job.id).status == "done"
    assert JobQueue(engine).recover() == Recovery(requeued=0, failed=0)
    assert state(engine, document_id) == ("processing", None)
    assert other_id != document_id


@pytest.mark.parametrize("max_attempts", [1, 2])
def test_a_chunk_job_without_pages_records_its_failed_attempt_itself(
    library: Library, engine: Engine, max_attempts: int
) -> None:
    document_id, _, sha256 = imported(library, extracted=False)
    queue = JobQueue(engine)
    queue.enqueue(document_id, CHUNK_STAGE, sha256, max_attempts=max_attempts)
    job = queue.claim(WORKER, [CHUNK_STAGE])
    assert job is not None
    completion = chunk_stage.handler(engine, queue)(job)
    row = job_row(engine, job.id)
    assert row.last_error == "PagesMissingError"
    if max_attempts == 1:
        assert completion is Completion.ATTEMPT_FAILED
        assert row.status == "failed"
        assert state(engine, document_id) == ("failed", None)
    else:
        assert completion is Completion.ATTEMPT_QUEUED
        assert row.status == "queued"
        assert state(engine, document_id) == ("processing", None)


def before_the_write_lock(monkeypatch: pytest.MonkeyPatch, meanwhile: Callable[[], None]) -> None:
    """Run ``meanwhile`` once, as another writer would, after the stage's snapshot and before it
    takes the write lock."""
    real = vault.db.engine.write_transaction
    pending = [meanwhile]

    def interfere_then_lock(engine: Engine) -> Any:
        while pending:
            pending.pop()()
        return real(engine)

    monkeypatch.setattr("vault.ingest.chunk_stage.write_transaction", interfere_then_lock)


def move_version(engine: Engine, version_id: str, sha256: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(document_versions)
            .where(document_versions.c.id == version_id)
            .values(blob_sha256=sha256)
        )


def test_a_version_that_comes_back_to_the_jobs_bytes_after_the_snapshot_is_chunked(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The snapshot finds the document holding other bytes; before the stage decides, the version
    # moves back to the job's bytes. An enqueue of that work would have returned this running job,
    # so ending it as stale would leave the document never chunked.
    document_id, version_id, sha256 = imported(library)
    _, other_version, _ = imported(library)
    move_version(engine, version_id, stored_sha(engine, other_version))
    queue, job = last_attempt(engine, document_id, sha256)
    assert queue.enqueue(document_id, CHUNK_STAGE, sha256) == job.id  # the same work, running
    before_the_write_lock(monkeypatch, lambda: move_version(engine, version_id, sha256))
    assert chunk_stage.handler(engine, queue)(job) is Completion.RECORDED
    assert stored_chunks(engine, version_id) != []
    assert job_row(engine, job.id).status == "done"
    assert state(engine, document_id) == ("searchable", None)


def test_pages_stored_after_a_snapshot_without_them_are_chunked(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, version_id, sha256 = imported(library, extracted=False)
    queue, job = last_attempt(engine, document_id, sha256)
    before_the_write_lock(
        monkeypatch,
        lambda: replace_pages(engine, version_id, course_pages(), "text-1", "utf-8"),
    )
    assert chunk_stage.handler(engine, queue)(job) is Completion.RECORDED
    assert stored_chunks(engine, version_id) != []
    assert state(engine, document_id) == ("searchable", None)


def test_a_job_ended_as_stale_lets_the_same_work_be_queued_again(
    library: Library, engine: Engine
) -> None:
    document_id, version_id, sha256 = imported(library)
    _, other_version, _ = imported(library)
    move_version(engine, version_id, stored_sha(engine, other_version))
    queue, job = last_attempt(engine, document_id, sha256)
    assert chunk_stage.handler(engine, queue)(job) is Completion.RECORDED
    move_version(engine, version_id, sha256)
    assert (
        queue.enqueue(document_id, CHUNK_STAGE, sha256) != job.id
    )  # a new job, not the closed one
