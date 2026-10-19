"""The persistent job queue: enqueue, claim, complete, fail and back off, retry, recover."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from vault.core.config import Settings
from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.db.schema import JOB_STAGES, blobs, document_versions, documents, jobs
from vault.jobs.queue import (
    BACKOFF_CAP,
    DEFAULT_MAX_ATTEMPTS,
    INTERRUPTED,
    UNNAMED_ERROR,
    JobLostError,
    JobQueue,
    backoff,
    error_name,
)
from vault.main import create_app

START = datetime(2026, 10, 15, 12, 0, 0)
HASH = "ab" * 32
OTHER_HASH = "cd" * 32


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "vault.db"
    upgrade_to_latest(path)
    return path


@pytest.fixture
def engine(db_file: Path) -> Iterator[Engine]:
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def queue(engine: Engine, clock: Clock) -> JobQueue:
    return JobQueue(engine, clock=clock)


def add_document(engine: Engine) -> str:
    with engine.begin() as connection:
        if connection.execute(select(blobs.c.sha256)).first() is None:
            connection.execute(
                insert(blobs).values(
                    sha256=HASH, size=1, ext="txt", mime="text/plain", created_at=START
                )
            )
        document_id = ids.new_ulid()
        version_id = ids.new_ulid()
        connection.execute(
            insert(documents).values(
                id=document_id, state="processing", local_only=False, created_at=START
            )
        )
        connection.execute(
            insert(document_versions).values(
                id=version_id,
                document_id=document_id,
                blob_sha256=HASH,
                version_no=1,
                origin="import",
                created_at=START,
            )
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(current_version_id=version_id)
        )
    return document_id


def status_of(engine: Engine, job_id: str) -> str:
    with engine.connect() as connection:
        value: str = connection.execute(
            select(jobs.c.status).where(jobs.c.id == job_id)
        ).scalar_one()
    return value


def row_of(engine: Engine, job_id: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(select(jobs).where(jobs.c.id == job_id)).one()


WORKER = ids.new_ulid()
OTHER_WORKER = ids.new_ulid()


# --- enqueue -----------------------------------------------------------------------------------


def test_an_enqueued_job_waits_with_no_attempts(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    row = row_of(engine, job_id)
    assert ids.is_ulid(job_id)
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert row.run_after == START


def test_the_same_work_is_queued_once(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    first = queue.enqueue(document_id, "extract", HASH)
    assert queue.enqueue(document_id, "extract", HASH) == first
    queue.claim(WORKER, ["extract"])
    assert queue.enqueue(document_id, "extract", HASH) == first  # running counts as open
    assert queue.counts() == {("extract", "running"): 1}


def test_other_work_is_queued_separately(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    other_document = add_document(engine)
    first = queue.enqueue(document_id, "extract", HASH)
    assert queue.enqueue(document_id, "ocr", HASH) != first
    assert queue.enqueue(document_id, "extract", OTHER_HASH) != first
    assert queue.enqueue(other_document, "extract", HASH) != first
    assert queue.counts() == {("extract", "queued"): 3, ("ocr", "queued"): 1}


def test_finished_work_can_be_queued_again(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    first = queue.enqueue(document_id, "extract", HASH)
    queue.claim(WORKER, ["extract"])
    queue.complete(first, WORKER)
    assert queue.enqueue(document_id, "extract", HASH) != first


@pytest.mark.parametrize(
    ("stage", "input_hash", "max_attempts"),
    [
        ("upload", HASH, 3),
        ("", HASH, 3),
        ("extract", HASH.upper(), 3),
        ("extract", HASH + "\n", 3),
        ("extract", HASH[:-1], 3),
        ("extract", HASH, 0),
    ],
)
def test_enqueue_refuses_bad_arguments(
    engine: Engine, queue: JobQueue, stage: str, input_hash: str, max_attempts: int
) -> None:
    document_id = add_document(engine)
    with pytest.raises(ValueError, match=r"stage|input_hash|max_attempts"):
        queue.enqueue(document_id, stage, input_hash, max_attempts=max_attempts)


def test_a_job_for_no_document_is_refused(queue: JobQueue) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        queue.enqueue(ids.new_ulid(), "extract", HASH)


def test_deleting_a_document_deletes_its_jobs(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    queue.enqueue(document_id, "extract", HASH)
    with engine.begin() as connection:
        connection.execute(documents.delete())
    assert queue.counts() == {}


# --- claim -------------------------------------------------------------------------------------


def test_claim_hands_out_a_job_once(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    job = queue.claim(WORKER, ["extract"])
    assert job is not None
    assert (job.id, job.document_id, job.stage, job.input_hash) == (
        job_id,
        document_id,
        "extract",
        HASH,
    )
    assert job.attempts == 1
    row = row_of(engine, job_id)
    assert row.locked_by == WORKER
    assert row.locked_at == START
    assert queue.claim(OTHER_WORKER, ["extract"]) is None


def test_claim_takes_only_the_stages_asked_for(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    queue.enqueue(document_id, "ocr", HASH)
    assert queue.claim(WORKER, ["extract", "embed"]) is None
    job = queue.claim(WORKER, ["ocr"])
    assert job is not None
    assert job.stage == "ocr"


def test_claim_takes_priority_then_the_longest_ready(
    engine: Engine, queue: JobQueue, clock: Clock
) -> None:
    document_id = add_document(engine)
    early = queue.enqueue(document_id, "extract", HASH)
    clock.advance(timedelta(seconds=1))
    later = queue.enqueue(document_id, "ocr", HASH)
    clock.advance(timedelta(seconds=1))
    urgent = queue.enqueue(document_id, "chunk", HASH, priority=5)
    order = []
    for _ in range(3):
        job = queue.claim(WORKER, JOB_STAGES)
        assert job is not None
        order.append(job.id)
    assert order == [urgent, early, later]


def test_a_job_is_not_claimed_before_its_time(
    engine: Engine, queue: JobQueue, clock: Clock
) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    with engine.begin() as connection:
        connection.execute(
            update(jobs).where(jobs.c.id == job_id).values(run_after=START + timedelta(minutes=1))
        )
    assert queue.claim(WORKER, ["extract"]) is None
    clock.advance(timedelta(minutes=1))
    assert queue.claim(WORKER, ["extract"]) is not None


@pytest.mark.parametrize("worker", ["worker-1", "", WORKER.lower(), WORKER + "\n"])
def test_claim_refuses_a_worker_id_that_is_not_a_ulid(queue: JobQueue, worker: str) -> None:
    with pytest.raises(ValueError, match="ULID"):
        queue.claim(worker, ["extract"])


@pytest.mark.parametrize("stages", [[], ["upload"], ["extract", "nope"]])
def test_claim_refuses_unknown_stages(queue: JobQueue, stages: list[str]) -> None:
    with pytest.raises(ValueError, match="stages"):
        queue.claim(WORKER, stages)


def test_workers_in_many_threads_never_share_a_job(db_file: Path, engine: Engine) -> None:
    document_ids = [add_document(engine) for _ in range(10)]
    queue = JobQueue(engine)
    for document_id in document_ids:
        for stage in ("extract", "ocr", "chunk"):
            queue.enqueue(document_id, stage, HASH)
    claimed: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        own_engine = create_vault_engine(db_file)  # a connection of its own, as a worker has
        try:
            own = JobQueue(own_engine)
            worker = ids.new_ulid()
            while (job := own.claim(worker, ["extract", "ocr", "chunk"])) is not None:
                with lock:
                    claimed.append(job.id)
                own.complete(job.id, worker)
        finally:
            own_engine.dispose()

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == 30
    assert len(set(claimed)) == 30
    assert queue.counts() == {(stage, "done"): 10 for stage in ("extract", "ocr", "chunk")}


# --- complete and fail -------------------------------------------------------------------------


def test_complete_marks_the_job_done(engine: Engine, queue: JobQueue, clock: Clock) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    queue.claim(WORKER, ["extract"])
    clock.advance(timedelta(seconds=5))
    queue.complete(job_id, WORKER)
    row = row_of(engine, job_id)
    assert row.status == "done"
    assert row.finished_at == START + timedelta(seconds=5)
    assert row.locked_by is None


def test_only_the_worker_that_holds_a_job_can_end_it(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    with pytest.raises(JobLostError):
        queue.complete(job_id, WORKER)  # not claimed yet
    queue.claim(WORKER, ["extract"])
    with pytest.raises(JobLostError):
        queue.complete(job_id, OTHER_WORKER)
    with pytest.raises(JobLostError):
        queue.fail(job_id, OTHER_WORKER, ValueError("x"))
    queue.complete(job_id, WORKER)
    with pytest.raises(JobLostError):
        queue.complete(job_id, WORKER)  # twice
    assert status_of(engine, job_id) == "done"


def test_release_gives_an_unstarted_job_back_with_its_attempt(
    engine: Engine, queue: JobQueue
) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    job = queue.claim(WORKER, ["extract"])
    assert job is not None
    queue.release(job_id, WORKER)
    row = row_of(engine, job_id)
    assert (row.status, row.attempts, row.locked_by, row.locked_at) == ("queued", 0, None, None)
    again = queue.claim(WORKER, ["extract"])  # ready at once, no back-off
    assert again is not None
    assert again.attempts == 1


def test_only_the_worker_that_holds_a_job_can_release_it(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    with pytest.raises(JobLostError):
        queue.release(job_id, WORKER)  # queued, not held
    assert queue.claim(WORKER, ["extract"]) is not None
    with pytest.raises(JobLostError):
        queue.release(job_id, OTHER_WORKER)
    queue.complete(job_id, WORKER)
    with pytest.raises(JobLostError):
        queue.release(job_id, WORKER)  # done
    assert status_of(engine, job_id) == "done"


def test_a_failed_attempt_waits_and_runs_again(
    engine: Engine, queue: JobQueue, clock: Clock
) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    queue.claim(WORKER, ["extract"])
    assert queue.fail(job_id, WORKER, ValueError("boom")) == "queued"
    row = row_of(engine, job_id)
    assert row.last_error == "ValueError"
    assert row.run_after == START + backoff(1)
    assert row.locked_by is None
    assert queue.claim(WORKER, ["extract"]) is None  # still backing off
    clock.advance(backoff(1))
    job = queue.claim(WORKER, ["extract"])
    assert job is not None
    assert job.attempts == 2


def test_the_last_failed_attempt_fails_the_job(
    engine: Engine, queue: JobQueue, clock: Clock
) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH, max_attempts=2)
    for expected in ("queued", "failed"):
        job = queue.claim(WORKER, ["extract"])
        assert job is not None
        assert queue.fail(job_id, WORKER, OSError("locked")) == expected
        clock.advance(BACKOFF_CAP)
    row = row_of(engine, job_id)
    assert row.status == "failed"
    assert row.attempts == 2
    assert row.finished_at is not None
    assert queue.claim(WORKER, ["extract"]) is None


def test_the_error_message_is_never_stored(engine: Engine, queue: JobQueue) -> None:
    document_text = "Patient diagnosis: confidential"
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    queue.claim(WORKER, ["extract"])
    queue.fail(job_id, WORKER, ValueError(document_text))
    with engine.connect() as connection:
        dump = "\n".join(connection.connection.iterdump())
    assert "Patient" not in dump
    assert "ValueError" in dump


def test_an_error_class_with_an_odd_name_is_stored_as_error() -> None:
    odd = type("Fehlerä", (Exception,), {})
    assert error_name(odd()) == UNNAMED_ERROR
    long = type("E" * 101, (Exception,), {})
    assert error_name(long()) == UNNAMED_ERROR
    assert error_name(KeyError()) == "KeyError"


def test_backoff_doubles_up_to_an_hour() -> None:
    assert [backoff(n) for n in (1, 2, 3)] == [
        timedelta(seconds=30),
        timedelta(seconds=60),
        timedelta(seconds=120),
    ]
    assert backoff(8) == BACKOFF_CAP
    assert backoff(10_000) == BACKOFF_CAP
    with pytest.raises(ValueError, match="attempts"):
        backoff(0)


# --- retry and recovery ------------------------------------------------------------------------


def fail_for_good(queue: JobQueue, document_id: str, stage: str = "extract") -> str:
    job_id = queue.enqueue(document_id, stage, HASH, max_attempts=1)
    queue.claim(WORKER, [stage])
    assert queue.fail(job_id, WORKER, RuntimeError()) == "failed"
    return job_id


def test_retry_queues_a_failed_job_with_fresh_attempts(engine: Engine, queue: JobQueue) -> None:
    job_id = fail_for_good(queue, add_document(engine))
    assert queue.retry(job_id)
    row = row_of(engine, job_id)
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.finished_at is None
    assert queue.claim(WORKER, ["extract"]) is not None


def test_retry_does_nothing_to_a_job_that_has_not_failed(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    assert not queue.retry(job_id)
    assert not queue.retry(ids.new_ulid())


def test_retry_gives_way_to_the_same_work_queued_since(engine: Engine, queue: JobQueue) -> None:
    document_id = add_document(engine)
    job_id = fail_for_good(queue, document_id)
    newer = queue.enqueue(document_id, "extract", HASH)
    assert not queue.retry(job_id)
    assert status_of(engine, job_id) == "failed"
    assert status_of(engine, newer) == "queued"


def test_recover_takes_back_jobs_a_killed_process_left_running(
    engine: Engine, queue: JobQueue, clock: Clock
) -> None:
    document_id = add_document(engine)
    alive = queue.enqueue(document_id, "extract", HASH)
    last_try = queue.enqueue(document_id, "ocr", HASH, max_attempts=1)
    waiting = queue.enqueue(document_id, "chunk", HASH)
    queue.claim(WORKER, ["extract"])
    queue.claim(WORKER, ["ocr"])
    clock.advance(timedelta(minutes=5))
    recovery = queue.recover()
    assert (recovery.requeued, recovery.failed) == (1, 1)
    row = row_of(engine, alive)
    assert row.status == "queued"
    assert row.last_error == INTERRUPTED
    assert row.attempts == 1  # the interrupted attempt counts
    assert row.run_after == clock.now
    assert status_of(engine, last_try) == "failed"
    assert status_of(engine, waiting) == "queued"
    with pytest.raises(JobLostError):
        queue.complete(alive, WORKER)  # the old worker's claim is gone
    assert queue.recover().requeued == 0  # safe to repeat


def test_the_app_recovers_jobs_at_start_up(vault_dir: Path) -> None:
    settings = Settings.model_validate({"vault": {"path": str(vault_dir)}})
    app = create_app(settings)
    engine: Engine = app.state.engine
    document_id = add_document(engine)
    queue = JobQueue(engine)
    job_id = queue.enqueue(document_id, "extract", HASH)
    queue.claim(WORKER, ["extract"])
    engine.dispose()  # the process is "killed" with the job running

    restarted = create_app(settings)
    try:
        assert status_of(restarted.state.engine, job_id) == "queued"
    finally:
        restarted.state.engine.dispose()


# --- what the database refuses -------------------------------------------------------------------


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'paused'",
        "stage = 'upload'",
        "attempts = -1",
        "attempts = max_attempts + 1",
        "max_attempts = 0",
        "input_hash = upper(input_hash)",
        "last_error = 'Patient diagnosis'",
        "last_error = ''",
        "status = 'running'",  # running with no owner
        "locked_by = '" + ids.new_ulid() + "', locked_at = '2026-10-15 12:00:00'",  # owner, queued
        "finished_at = '2026-10-15 12:00:00'",  # finished but queued
        "status = 'done'",  # done without a finish time
    ],
)
def test_the_database_refuses_an_inconsistent_job(
    engine: Engine, queue: JobQueue, assignment: str
) -> None:
    job_id = queue.enqueue(add_document(engine), "extract", HASH)
    # The assignments are fixed text in this file.
    statement = text(f"UPDATE jobs SET {assignment} WHERE id = :id")  # noqa: S608
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(statement, {"id": job_id})


def test_the_database_refuses_a_worker_id_that_is_not_a_ulid(
    engine: Engine, queue: JobQueue
) -> None:
    job_id = queue.enqueue(add_document(engine), "extract", HASH)
    with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status = 'running', locked_by = 'worker-1',"
                " locked_at = '2026-10-15 12:00:00' WHERE id = :id"
            ),
            {"id": job_id},
        )


def document_state(engine: Engine, document_id: str) -> tuple[str, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            select(documents.c.state, documents.c.state_before_trash).where(
                documents.c.id == document_id
            )
        ).one()
    return row.state, row.state_before_trash


def trash(engine: Engine, document_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(state="trashed", state_before_trash=documents.c.state, trashed_at=START)
        )


@pytest.mark.parametrize("before", ["processing", "searchable", "ready", "failed"])
def test_a_job_that_uses_up_its_attempts_fails_its_document(
    engine: Engine, queue: JobQueue, before: str
) -> None:
    document_id = add_document(engine)
    other = add_document(engine)
    with engine.begin() as connection:
        connection.execute(update(documents).values(state=before))
    job_id = queue.enqueue(document_id, "chunk", HASH, max_attempts=2)
    queue.claim(WORKER, ["chunk"])
    assert queue.fail(job_id, WORKER, RuntimeError()) == "queued"
    assert document_state(engine, document_id) == (before, None)  # attempts are left
    with engine.begin() as connection:
        connection.execute(update(jobs).values(run_after=START))
    queue.claim(WORKER, ["chunk"])
    assert queue.fail(job_id, WORKER, RuntimeError()) == "failed"
    assert document_state(engine, document_id) == ("failed", None)
    assert document_state(engine, other) == (before, None)


def test_a_document_in_the_trash_keeps_failed_for_its_restore(
    engine: Engine, queue: JobQueue
) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "chunk", HASH, max_attempts=1)
    queue.claim(WORKER, ["chunk"])
    trash(engine, document_id)
    assert queue.fail(job_id, WORKER, RuntimeError()) == "failed"
    assert document_state(engine, document_id) == ("trashed", "failed")


def test_recovery_that_fails_a_job_fails_its_document(engine: Engine, queue: JobQueue) -> None:
    last_try, retried, trashed = (add_document(engine) for _ in range(3))
    queue.enqueue(last_try, "extract", HASH, max_attempts=1)
    queue.enqueue(retried, "extract", HASH)
    queue.enqueue(trashed, "extract", HASH, max_attempts=1)
    for _ in range(3):
        assert queue.claim(WORKER, ["extract"]) is not None
    trash(engine, trashed)
    assert queue.recover().failed == 2
    assert document_state(engine, last_try) == ("failed", None)
    assert document_state(engine, retried) == ("processing", None)
    assert document_state(engine, trashed) == ("trashed", "failed")
