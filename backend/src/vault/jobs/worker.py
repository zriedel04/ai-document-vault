"""The worker pool (ADR 0005, plan sections 4 and 7): threads in the server process that run jobs.

Each registered stage gets as many threads as its concurrency limit (extraction 2, OCR 1,
embedding 1, AI enrichment 2 by default; ``[workers]`` in ``config.toml``), so a stage can never
run more jobs at once than its limit, whatever the others do. A thread claims one job of its own
stage from the queue, runs the stage's handler, and reports the outcome:

- the handler returns: the job is ``complete``, unless the handler returned a ``Completion``:
  it marked the job done (``RECORDED``) or recorded its failed attempt (``ATTEMPT_QUEUED``,
  ``ATTEMPT_FAILED``) itself, in the transaction that decided it, so that nothing can come
  between the decision and the job's end (a handler that writes to ``vault.db`` or decides from
  it must do this; see ``vault.jobs.queue.Completion``);
- the handler raises: the attempt is recorded with ``fail``, which stores only the exception's
  class name and queues the job again after a back-off, or fails it after its last attempt. Every
  exception is caught, ``BaseException`` too: an exception that left the thread would end it (the
  stage would quietly lose a worker) and Python's thread hook would print its message, which can
  quote document text;
- the queue says the worker no longer holds the job (``JobLostError``: a retry or start-up
  recovery took it back): the outcome is dropped, since the job belongs to someone else now.

A stage with no handler is never claimed: its jobs wait in the queue until a later version
registers one. A thread with nothing to do sleeps until ``wake()`` (call it after queueing work)
or until ``idle_seconds`` have passed, which is how a job whose back-off has ended is found; it
never spins on the database.

``pause()`` stops threads from claiming new jobs; jobs already running finish. ``resume()``
undoes it. ``stop()`` asks every thread to end after its current job and waits for them. A claim
can be on its way when either is called (it waits on SQLite's busy timeout, outside the lock), so
claims are counted: ``pause()`` returns only once none is in progress, and a claim that comes back
after a pause or stop began gives its job back unstarted (``JobQueue.release``), with the attempt
it counted. So once ``pause()`` returns, every job is either one that was already running or
back in the queue, and no claim made after ``stop()`` was called runs. A job
still running when the process exits stays ``running`` in the database and start-up recovery
(``JobQueue.recover``, called by ``create_app`` before the pool starts) queues it again.

Nothing here logs a job id, a document id or an error message: only stage names (fixed in code),
counts and outcomes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Final

from vault.core.logging import log_event, metric
from vault.db import ids
from vault.db.schema import JOB_STAGES
from vault.jobs.queue import Completion, Job, JobLostError, JobQueue

logger = logging.getLogger(__name__)

# Returns a Completion when it ended the attempt in its own transaction, else None.
type Handler = Callable[[Job], Completion | None]

# The plan's limits (section 7): extraction 2, OCR 1, embedding 1, AI 2; the rest one at a time.
DEFAULT_LIMITS: Final[Mapping[str, int]] = {
    "extract": 2,
    "ocr": 1,
    "metadata": 1,
    "chunk": 1,
    "embed": 1,
    "enrich": 2,
}
MAX_LIMIT: Final = 16
IDLE_SECONDS: Final = 5.0


class Stage(StrEnum):
    """The job stages as an enum, so a stage name can be logged (a member is fixed in code)."""

    EXTRACT = "extract"
    OCR = "ocr"
    METADATA = "metadata"
    CHUNK = "chunk"
    EMBED = "embed"
    ENRICH = "enrich"


class Outcome(StrEnum):
    DONE = "done"
    RETRY = "retry"
    FAILED = "failed"
    LOST = "lost"


class WorkerPool:
    """Threads that run the jobs of the stages in ``handlers``, at most ``limits[stage]`` each."""

    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        limits: Mapping[str, int] = DEFAULT_LIMITS,
        *,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        unknown = set(handlers) - set(JOB_STAGES)
        if unknown:
            msg = "handlers may only be registered for the known job stages"
            raise ValueError(msg)
        for stage in handlers:
            limit = limits.get(stage)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
                msg = f"each registered stage needs a limit from 1 to {MAX_LIMIT}"
                raise ValueError(msg)
        if not idle_seconds > 0:
            msg = "idle_seconds must be positive"
            raise ValueError(msg)
        self._queue = queue
        self._handlers = dict(handlers)
        self._limits = {stage: limits[stage] for stage in handlers}
        self._idle_seconds = idle_seconds
        lock = threading.Lock()
        self._condition = threading.Condition(lock)
        # The same lock; notified when a claim ends, so pause() waits without waking idle threads.
        self._claim_ended = threading.Condition(lock)
        self._signal = 0  # raised by wake(), so a sleeping thread knows something changed
        self._paused = False
        self._stopping = False
        self._claiming = 0  # claims in progress, which pause() waits for
        self._threads: list[threading.Thread] = []
        self._started = False

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        return tuple(self._threads)

    @property
    def paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        """Start one thread per allowed concurrent job of each registered stage. Once only."""
        with self._condition:
            if self._started:
                msg = "a worker pool starts once"
                raise RuntimeError(msg)
            self._started = True
        for stage, limit in self._limits.items():
            for _ in range(limit):
                thread = threading.Thread(
                    target=self._run, args=(stage, ids.new_ulid()), daemon=True
                )
                self._threads.append(thread)
                thread.start()
        log_event(logger, "workers.started", threads=metric(len(self._threads)))

    def wake(self) -> None:
        """Tell idle threads to look for work now (call it after queueing a job)."""
        with self._condition:
            self._signal += 1
            self._condition.notify_all()

    def pause(self) -> None:
        """Claim no new jobs until ``resume()``. Jobs already running finish; no job starts after
        this returns. Waits for claims in progress, which give their job back."""
        with self._condition:
            self._paused = True
            while self._claiming:
                self._claim_ended.wait()
        log_event(logger, "workers.paused")

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._signal += 1
            self._condition.notify_all()
        log_event(logger, "workers.resumed")

    def stop(self, timeout: float = 10.0) -> bool:
        """Ask every thread to end after its current job and wait up to ``timeout`` seconds in
        all. True if every thread ended; a thread still running a job is left to finish on its own
        (the threads are daemons, so they do not keep the process alive)."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        stopped = not any(thread.is_alive() for thread in self._threads)
        log_event(logger, "workers.stopped", all_ended=stopped)
        return stopped

    # --- the thread ---------------------------------------------------------------------------

    def _run(self, stage: str, worker_id: str) -> None:
        handler = self._handlers[stage]
        while True:
            with self._condition:
                while self._paused and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                signal = self._signal
                self._claiming += 1
            job = self._claim(stage, worker_id)
            if job is None:
                self._sleep(signal)
                continue
            outcome = self._work(handler, job, worker_id)
            log_event(logger, "workers.job_ended", stage=Stage(stage), outcome=outcome)

    def _claim(self, stage: str, worker_id: str) -> Job | None:
        """Claim a job, counted in ``_claiming`` until it is kept or given back. Keeping it is
        decided under the lock in the same step that ends the count, so ``pause()`` cannot return
        between the decision and the count; a job claimed after a pause or stop began goes back."""
        job: Job | None = None
        kept = False
        try:
            try:
                job = self._queue.claim(worker_id, (stage,))
            except Exception:  # the database is busy or gone: try again after a pause
                log_event(logger, "workers.claim_failed", level=logging.WARNING, stage=Stage(stage))
                return None
            with self._condition:
                if job is None or not (self._paused or self._stopping):
                    kept = True
                    self._claiming -= 1
                    self._claim_ended.notify_all()
                    return job
            try:
                self._queue.release(job.id, worker_id)
            except Exception:  # it stays running; start-up recovery queues it again
                log_event(
                    logger, "workers.release_failed", level=logging.WARNING, stage=Stage(stage)
                )
            return None
        finally:
            if not kept:
                with self._condition:
                    self._claiming -= 1
                    self._claim_ended.notify_all()

    def _sleep(self, signal: int) -> None:
        """Wait until ``wake()``, ``resume()``, ``stop()`` or ``idle_seconds``, whichever comes
        first."""
        with self._condition:
            if self._signal == signal and not self._stopping:
                self._condition.wait(self._idle_seconds)

    def _work(self, handler: Handler, job: Job, worker_id: str) -> Outcome:
        try:
            completion = handler(job)
        except BaseException as error:  # every exception: see the module docstring
            return self._report_failure(job, worker_id, error)
        if completion is Completion.RECORDED:
            return Outcome.DONE
        if completion is Completion.ATTEMPT_QUEUED:
            return Outcome.RETRY
        if completion is Completion.ATTEMPT_FAILED:
            return Outcome.FAILED
        try:
            self._queue.complete(job.id, worker_id)
        except JobLostError:
            return Outcome.LOST
        except Exception:
            # The job stays running; start-up recovery queues it again.
            log_event(logger, "workers.report_failed", level=logging.WARNING)
            return Outcome.LOST
        return Outcome.DONE

    def _report_failure(self, job: Job, worker_id: str, error: BaseException) -> Outcome:
        try:
            status = self._queue.fail(job.id, worker_id, error)
        except JobLostError:
            return Outcome.LOST
        except Exception:
            log_event(logger, "workers.report_failed", level=logging.WARNING)
            return Outcome.LOST
        return Outcome.FAILED if status == "failed" else Outcome.RETRY
