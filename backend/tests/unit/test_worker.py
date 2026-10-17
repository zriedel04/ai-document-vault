import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from vault.core.config import Settings, WorkerSettings
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.db.schema import JOB_STAGES, jobs
from vault.jobs.queue import Job, JobQueue
from vault.jobs.worker import DEFAULT_LIMITS, Stage, WorkerPool
from vault.main import create_app

from .test_jobs import Clock, add_document

SECRET = "Patient diagnosis confidential"  # noqa: S105 - document text, not a password


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    path = tmp_path / "vault.db"
    upgrade_to_latest(path)
    engine = create_vault_engine(path)
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


@pytest.fixture
def pools() -> Iterator[list[WorkerPool]]:
    """Pools a test made; each is stopped at the end, so no thread outlives its test."""
    made: list[WorkerPool] = []
    yield made
    for pool in made:
        pool.stop(5)


def enqueue(engine: Engine, queue: JobQueue, count: int, stage: str = "extract") -> list[str]:
    document_id = add_document(engine)
    return [queue.enqueue(document_id, stage, f"{n:064x}") for n in range(count)]


def statuses(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        return {row.id: row.status for row in connection.execute(select(jobs.c.id, jobs.c.status))}


def last_error(engine: Engine, job_id: str) -> str | None:
    with engine.connect() as connection:
        value: str | None = connection.execute(
            select(jobs.c.last_error).where(jobs.c.id == job_id)
        ).scalar_one()
    return value


def until(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


class Gate:
    """A handler that holds each job until released, and counts how many it holds at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.most = 0
        self.seen: list[str] = []
        self.release = threading.Event()

    def __call__(self, job: Job) -> None:
        with self.lock:
            self.active += 1
            self.most = max(self.most, self.active)
            self.seen.append(job.id)
        try:
            assert self.release.wait(10)
        finally:
            with self.lock:
                self.active -= 1


def start(pools: list[WorkerPool], pool: WorkerPool) -> WorkerPool:
    pools.append(pool)
    pool.start()
    return pool


# --- running jobs ------------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", range(5))
def test_a_stage_never_runs_more_jobs_at_once_than_its_limit(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool], attempt: int
) -> None:
    job_ids = enqueue(engine, queue, 6)
    gate = Gate()
    start(pools, WorkerPool(queue, {"extract": gate}, {"extract": 2}, idle_seconds=0.05))

    until(lambda: gate.active == 2)
    time.sleep(0.1)  # time for a third thread to claim, if there were one
    assert gate.active == 2
    gate.release.set()
    until(lambda: all(statuses(engine)[job] == "done" for job in job_ids))
    assert gate.most == 2
    assert sorted(gate.seen) == sorted(job_ids)  # each job ran once


def test_a_stage_without_a_handler_is_never_claimed(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool]
) -> None:
    [ocr_job] = enqueue(engine, queue, 1, stage="ocr")
    [extract_job] = enqueue(engine, queue, 1)
    start(pools, WorkerPool(queue, {"extract": lambda _job: None}, idle_seconds=0.05))

    until(lambda: statuses(engine)[extract_job] == "done")
    time.sleep(0.1)
    assert statuses(engine)[ocr_job] == "queued"


def test_a_pool_with_no_handlers_starts_no_threads(
    queue: JobQueue, pools: list[WorkerPool]
) -> None:
    pool = start(pools, WorkerPool(queue, {}))
    assert pool.threads == ()
    assert pool.stop(1)


def test_each_thread_claims_under_its_own_worker_id(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool]
) -> None:
    enqueue(engine, queue, 3)
    gate = Gate()
    start(pools, WorkerPool(queue, {"extract": gate}, {"extract": 3}, idle_seconds=0.05))
    until(lambda: gate.active == 3)
    with engine.connect() as connection:
        holders: list[str | None] = list(
            connection.execute(select(jobs.c.locked_by).where(jobs.c.status == "running")).scalars()
        )
        assert len(set(holders)) == 3
    gate.release.set()


# --- failures ----------------------------------------------------------------------------------


def test_a_failed_attempt_is_recorded_by_class_name_and_runs_again_after_its_back_off(
    engine: Engine,
    queue: JobQueue,
    clock: Clock,
    pools: list[WorkerPool],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    [job_id] = enqueue(engine, queue, 1)
    calls: list[int] = []

    def flaky(job: Job) -> None:
        calls.append(job.attempts)
        if job.attempts == 1:
            raise ValueError(SECRET)

    caplog.set_level(logging.DEBUG)
    pool = start(pools, WorkerPool(queue, {"extract": flaky}, idle_seconds=0.05))
    until(lambda: last_error(engine, job_id) == "ValueError")
    assert statuses(engine)[job_id] == "queued"
    time.sleep(0.15)
    assert calls == [1]  # waiting out its back-off

    clock.advance(timedelta(seconds=31))
    pool.wake()
    until(lambda: statuses(engine)[job_id] == "done")
    assert calls == [1, 2]
    output = capsys.readouterr()
    assert SECRET not in caplog.text + output.out + output.err


def test_the_last_failed_attempt_fails_the_job(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool]
) -> None:
    document_id = add_document(engine)
    job_id = queue.enqueue(document_id, "extract", "ab" * 32, max_attempts=1)

    def broken(_job: Job) -> None:
        raise RuntimeError(SECRET)

    start(pools, WorkerPool(queue, {"extract": broken}, idle_seconds=0.05))
    until(lambda: statuses(engine)[job_id] == "failed")
    assert last_error(engine, job_id) == "RuntimeError"


@pytest.mark.parametrize("error", [SystemExit(SECRET), KeyboardInterrupt(), GeneratorExit()])
def test_an_exception_outside_exception_is_recorded_and_the_thread_goes_on(
    engine: Engine,
    queue: JobQueue,
    pools: list[WorkerPool],
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
) -> None:
    first, second = enqueue(engine, queue, 2)

    def handler(job: Job) -> None:
        if job.id == first:
            raise error

    pool = start(pools, WorkerPool(queue, {"extract": handler}, {"extract": 1}, idle_seconds=0.05))
    until(lambda: statuses(engine)[second] == "done")
    assert last_error(engine, first) == type(error).__name__
    assert all(thread.is_alive() for thread in pool.threads)
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "Traceback" not in output.err


def test_a_job_taken_back_while_it_runs_is_left_to_its_new_owner(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool]
) -> None:
    first, second = enqueue(engine, queue, 2)

    def handler(job: Job) -> None:
        if job.id == first and job.attempts == 1:
            queue.recover()  # as a restart would: the job is queued again under no worker

    start(pools, WorkerPool(queue, {"extract": handler}, {"extract": 1}, idle_seconds=0.05))
    until(lambda: statuses(engine)[second] == "done")
    until(lambda: statuses(engine)[first] == "done")  # the requeued job ran again


def test_a_database_error_while_claiming_does_not_end_the_thread(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool]
) -> None:
    [job_id] = enqueue(engine, queue, 1)

    class Flaky(JobQueue):
        failures = 2

        def claim(self, worker_id: str, stages: Iterable[str]) -> Job | None:
            if self.failures:
                self.failures -= 1
                raise OSError(SECRET)
            return super().claim(worker_id, stages)

    flaky = Flaky(engine, clock=queue._clock)
    start(pools, WorkerPool(flaky, {"extract": lambda _job: None}, idle_seconds=0.02))
    until(lambda: statuses(engine)[job_id] == "done")


@pytest.mark.parametrize("step", ["complete", "fail"])
def test_a_database_error_while_reporting_leaves_the_job_for_recovery(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool], step: str
) -> None:
    first, second = enqueue(engine, queue, 2)

    class Broken(JobQueue):
        def complete(self, job_id: str, worker_id: str) -> None:
            if job_id == first:
                raise OSError(SECRET)
            super().complete(job_id, worker_id)

        def fail(self, job_id: str, worker_id: str, error: BaseException) -> str:
            if job_id == first:
                raise OSError(SECRET)
            return super().fail(job_id, worker_id, error)

    def handler(job: Job) -> None:
        if step == "fail" and job.id == first:
            raise ValueError(SECRET)

    broken = Broken(engine, clock=queue._clock)
    start(pools, WorkerPool(broken, {"extract": handler}, {"extract": 1}, idle_seconds=0.02))
    until(lambda: statuses(engine)[second] == "done")
    assert statuses(engine)[first] == "running"  # start-up recovery queues it again
    assert queue.recover().requeued == 1


# --- idling, waking, pausing and stopping ------------------------------------------------------


class CountingQueue(JobQueue):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.claims = 0

    def claim(self, worker_id: str, stages: Iterable[str]) -> Job | None:
        self.claims += 1
        return super().claim(worker_id, stages)


def test_an_idle_thread_sleeps_instead_of_asking_the_database_again(
    engine: Engine, pools: list[WorkerPool]
) -> None:
    counting = CountingQueue(engine)
    start(pools, WorkerPool(counting, {"extract": lambda _job: None}, {"extract": 2}))
    time.sleep(0.5)
    assert counting.claims == 2  # one look per thread, then sleep for idle_seconds


def test_wake_makes_an_idle_thread_look_for_work_at_once(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool]
) -> None:
    pool = start(pools, WorkerPool(queue, {"extract": lambda _job: None}, idle_seconds=60))
    time.sleep(0.1)  # the thread found nothing and sleeps for a minute
    [job_id] = enqueue(engine, queue, 1)
    pool.wake()
    until(lambda: statuses(engine)[job_id] == "done", seconds=2)


def test_pause_lets_running_jobs_finish_and_claims_nothing_new_until_resume(
    engine: Engine, queue: JobQueue, pools: list[WorkerPool]
) -> None:
    first, second = enqueue(engine, queue, 2)
    gate = Gate()
    pool = start(pools, WorkerPool(queue, {"extract": gate}, {"extract": 1}, idle_seconds=0.05))
    until(lambda: gate.active == 1)

    pool.pause()
    assert pool.paused
    gate.release.set()
    until(lambda: statuses(engine)[first] == "done")
    pool.wake()
    time.sleep(0.2)
    assert statuses(engine)[second] == "queued"

    pool.resume()
    until(lambda: statuses(engine)[second] == "done")


class HeldClaimQueue(JobQueue):
    """A queue whose claims wait until ``proceed`` is set, as a claim waits on SQLite's busy
    timeout outside the pool's lock."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def claim(self, worker_id: str, stages: Iterable[str]) -> Job | None:
        self.entered.set()
        assert self.proceed.wait(10)
        return super().claim(worker_id, stages)


def attempts(engine: Engine, job_id: str) -> int:
    with engine.connect() as connection:
        value: int = connection.execute(
            select(jobs.c.attempts).where(jobs.c.id == job_id)
        ).scalar_one()
    return value


@pytest.mark.parametrize("attempt", range(3))
def test_pause_waits_for_a_claim_in_progress_and_the_claimed_job_goes_back(
    engine: Engine, pools: list[WorkerPool], attempt: int
) -> None:
    held = HeldClaimQueue(engine)
    [job_id] = enqueue(engine, held, 1)
    ran: list[str] = []
    pool = start(
        pools, WorkerPool(held, {"extract": lambda job: ran.append(job.id)}, {"extract": 1})
    )
    assert held.entered.wait(5)  # the thread is inside claim()

    pausing = threading.Thread(target=pool.pause)
    pausing.start()
    pausing.join(0.2)
    assert pausing.is_alive()  # pause() waits for the claim
    held.proceed.set()
    pausing.join(5)
    assert not pausing.is_alive()

    time.sleep(0.2)
    assert ran == []  # nothing started after pause() returned
    assert statuses(engine)[job_id] == "queued"
    assert attempts(engine, job_id) == 0  # the given-back claim used no attempt

    pool.resume()
    until(lambda: statuses(engine)[job_id] == "done")
    assert ran == [job_id]


def test_a_claim_that_returns_after_stop_gives_its_job_back(engine: Engine) -> None:
    held = HeldClaimQueue(engine)
    [job_id] = enqueue(engine, held, 1)
    ran: list[str] = []
    pool = WorkerPool(held, {"extract": lambda job: ran.append(job.id)}, {"extract": 1})
    pool.start()
    assert held.entered.wait(5)

    assert not pool.stop(0.1)  # the thread is still inside claim()
    held.proceed.set()
    until(lambda: not any(thread.is_alive() for thread in pool.threads))
    assert ran == []
    assert statuses(engine)[job_id] == "queued"
    assert attempts(engine, job_id) == 0


def test_stop_ends_every_thread(engine: Engine, queue: JobQueue) -> None:
    pool = WorkerPool(queue, {"extract": lambda _job: None, "ocr": lambda _job: None})
    pool.start()
    assert len(pool.threads) == DEFAULT_LIMITS["extract"] + DEFAULT_LIMITS["ocr"]
    assert pool.stop(5)
    assert not any(thread.is_alive() for thread in pool.threads)


def test_stop_waits_for_a_running_job_only_as_long_as_it_is_told(
    engine: Engine, queue: JobQueue
) -> None:
    [job_id] = enqueue(engine, queue, 1)
    gate = Gate()
    pool = WorkerPool(queue, {"extract": gate}, {"extract": 1}, idle_seconds=0.05)
    pool.start()
    until(lambda: gate.active == 1)

    started = time.monotonic()
    assert not pool.stop(0.2)
    assert time.monotonic() - started < 2
    gate.release.set()
    until(lambda: not any(thread.is_alive() for thread in pool.threads))
    assert statuses(engine)[job_id] == "done"  # the job it was running still finished


def test_a_paused_pool_stops(queue: JobQueue) -> None:
    pool = WorkerPool(queue, {"extract": lambda _job: None})
    pool.start()
    pool.pause()
    assert pool.stop(5)


def test_a_pool_starts_once(queue: JobQueue, pools: list[WorkerPool]) -> None:
    pool = start(pools, WorkerPool(queue, {}))
    with pytest.raises(RuntimeError):
        pool.start()


# --- configuration -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handlers", "limits"),
    [
        ({"ingest": print}, DEFAULT_LIMITS),
        ({"extract": print}, {}),
        ({"extract": print}, {"extract": 0}),
        ({"extract": print}, {"extract": 17}),
        ({"extract": print}, {"extract": True}),
        ({"extract": print}, {"extract": 1.5}),
    ],
)
def test_a_pool_refuses_unknown_stages_and_bad_limits(
    queue: JobQueue, handlers: dict[str, Callable[[Job], None]], limits: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="stage"):
        WorkerPool(queue, handlers, limits)  # type: ignore[arg-type]


@pytest.mark.parametrize("seconds", [0, -1])
def test_idle_seconds_must_be_positive(queue: JobQueue, seconds: float) -> None:
    with pytest.raises(ValueError, match="idle_seconds"):
        WorkerPool(queue, {}, idle_seconds=seconds)


def test_every_stage_has_a_loggable_name_and_a_default_limit() -> None:
    assert [stage.value for stage in Stage] == list(JOB_STAGES)
    assert set(DEFAULT_LIMITS) == set(JOB_STAGES)
    assert WorkerSettings().limits() == dict(DEFAULT_LIMITS)


@pytest.mark.parametrize("value", [0, 17, True, "two"])
def test_worker_settings_refuse_bad_limits(value: object) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"workers": {"extract": value}})


def test_worker_settings_refuse_an_unknown_stage() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"workers": {"ingest": 1}})


# --- the app -----------------------------------------------------------------------------------


def test_the_app_starts_and_stops_its_pool_with_the_server(vault_dir: Path) -> None:
    app = create_app(Settings.model_validate({"vault": {"path": str(vault_dir)}}))
    pool = app.state.workers
    assert isinstance(pool, WorkerPool)
    with TestClient(app):
        assert pool.threads == ()  # no stage has a handler yet
    with pytest.raises(RuntimeError):
        pool.start()  # it was started by the server


def test_without_a_vault_there_is_no_pool() -> None:
    app = create_app(Settings())
    assert app.state.workers is None
    with TestClient(app):
        pass
