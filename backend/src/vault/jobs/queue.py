"""The persistent job queue (ADR 0005): a ``jobs`` table in ``vault.db``, no extra service.

A job is one processing stage (``extract``, ``ocr``, ...) for one document. Its life:

- ``enqueue`` adds it as ``queued``. The same document, stage and input already waiting or
  running is not added again; its id is returned instead.
- ``claim`` hands the most urgent ready job of the given stages to one worker and marks it
  ``running`` under that worker's id. It is one ``UPDATE ... RETURNING`` statement, so two workers
  (threads, or connections) can never claim the same job.
- ``complete`` marks it ``done``. A handler that stores its result in ``vault.db`` does it in the
  same transaction (``connection``), so a crash can never leave a stored result behind a job that
  is still ``running`` (see ``Completion``). ``fail`` records the exception's class name (never
  its message, which can quote document content) and queues it again after a back-off, or marks
  it ``failed`` once its attempts are used up, and the document ``failed`` with it in the same
  transaction (a document in the trash keeps ``failed`` as the state a restore brings back),
  unless a later job of the same stage has replaced it: that job decides the document's state.
  Both act only for the worker that holds the job, and say so (``JobLostError``) if it no longer
  does.
- ``release`` gives a claimed job back unstarted, with the attempt it counted returned (the
  worker pool calls it when it was paused or stopped while the claim was on its way).
- ``retry`` queues a failed job again with fresh attempts. ``retry_document`` does it for every
  stage of one document whose latest job failed (the owner's Retry button on a document's page).
- ``recover`` runs at start-up, before any worker: a job left ``running`` by a process that was
  killed is queued again, or failed if that attempt was its last (its document too, as ``fail``
  does). Nothing is lost by a crash, and nothing that succeeded is failed by one: a job still
  ``running`` has stored nothing, since a stage's result and its ``done`` commit together.

Times are naive UTC, as everywhere in ``vault.db``. The worker pool that calls this arrives with
the first stage that has work to do.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    FromClause,
    and_,
    case,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from vault.db import ids
from vault.db.engine import write_transaction
from vault.db.schema import JOB_STAGES, TRASHED, documents, jobs

DEFAULT_MAX_ATTEMPTS: Final = 3
BACKOFF_BASE: Final = timedelta(seconds=30)
BACKOFF_CAP: Final = timedelta(hours=1)
# What ``last_error`` may hold (the database checks the same rule). Use fullmatch.
ERROR_NAME: Final = re.compile(r"[A-Za-z0-9_]{1,100}")
# Written for an exception whose class name does not fit (Python allows any letter in a name).
UNNAMED_ERROR: Final = "Error"
# Written when the process ended while the job was running.
INTERRUPTED: Final = "Interrupted"
SHA256: Final = re.compile(r"[0-9a-f]{64}")
_OPEN: Final = ("queued", "running")
FAILED: Final = "failed"  # the document state for a stage that cannot complete


class JobError(Exception):
    """Base class for job queue errors. Messages never name a document or a file."""


class JobLostError(JobError):
    """The worker no longer holds the job (start-up recovery or a retry took it back)."""


@dataclass(frozen=True)
class Job:
    """A claimed job: what the worker needs to do the work and report back."""

    id: str
    worker_id: str  # the worker that holds it, which ``complete`` and ``fail`` need
    document_id: str
    stage: str
    input_hash: str
    attempts: int
    max_attempts: int


def utc_now() -> datetime:
    """The current time as the database stores it: UTC without a time zone."""
    return datetime.now(UTC).replace(tzinfo=None)


def backoff(attempts: int) -> timedelta:
    """How long a job waits after its ``attempts``-th failed attempt: 30 s, 1 min, 2 min, ...,
    at most one hour."""
    if attempts < 1:
        msg = "attempts starts at 1"
        raise ValueError(msg)
    # Capped before the power grows: 2**60 seconds would overflow a timedelta.
    delay: timedelta = BACKOFF_BASE * 2 ** min(attempts - 1, 20)
    return min(delay, BACKOFF_CAP)


def error_name(error: BaseException) -> str:
    """The exception's class name, or ``Error`` if it is not a plain ASCII name."""
    name = type(error).__name__
    return name if ERROR_NAME.fullmatch(name) else UNNAMED_ERROR


def _check_worker(worker_id: str) -> None:
    if not ids.is_ulid(worker_id):
        msg = "a worker id is a ULID (vault.db.ids.new_ulid)"
        raise ValueError(msg)


def _check_stages(stages: Iterable[str]) -> tuple[str, ...]:
    chosen = tuple(stages)
    if not chosen or any(stage not in JOB_STAGES for stage in chosen):
        msg = "stages must be one or more of the known job stages"
        raise ValueError(msg)
    return chosen


class Completion(StrEnum):
    """What a handler returns when it ended its own job's attempt.

    A handler that writes its result to ``vault.db`` calls ``JobQueue.complete`` with the
    connection of the transaction that writes it, and returns ``RECORDED``; the worker pool then
    does not complete the job again. The result and the job's ``done`` commit together, so there
    is no moment at which a crash leaves the result stored and the job ``running`` (start-up
    recovery would run it again, or on its last attempt fail it and its document). It also fences
    the write: a worker that no longer holds the job gets ``JobLostError`` inside the transaction,
    and its result is rolled back. A handler that stores nothing returns None, and the pool
    completes the job.

    The same holds for a failed attempt that a handler decides under its write lock: it calls
    ``JobQueue.fail`` with that transaction's connection and returns ``ATTEMPT_QUEUED`` or
    ``ATTEMPT_FAILED`` (from the status ``fail`` returned). Raising instead would end the
    transaction first and leave a moment in which another writer's ``enqueue`` of the same work
    returns this still running job, whose attempt has already decided without that work.
    """

    RECORDED = "recorded"  # the job is done
    ATTEMPT_QUEUED = "queued"  # the attempt failed; the job runs again after its back-off
    ATTEMPT_FAILED = "failed"  # the attempt failed and was the last: the job is failed


class RetryRefusal(StrEnum):
    """Why ``retry_document`` queued nothing although there may be failed jobs."""

    NOT_FOUND = "not_found"  # no such document (or not an id at all)
    IN_TRASH = "in_trash"  # a document in the trash is not processed; restore it first


@dataclass(frozen=True)
class Recovery:
    """What start-up recovery did: jobs queued again, and jobs failed for good."""

    requeued: int
    failed: int


def _replaced(job: FromClause, later: FromClause) -> ColumnElement[bool]:
    """A later job of the same document and stage exists: by the time it was queued, and the id
    (a ULID) breaks a tie in the same instant. The one rule for "the latest job of its stage", used
    by ``retry_document`` and by the document state a failed job sets."""
    return exists().where(
        later.c.document_id == job.c.document_id,
        later.c.stage == job.c.stage,
        or_(
            later.c.created_at > job.c.created_at,
            and_(later.c.created_at == job.c.created_at, later.c.id > job.c.id),
        ),
    )


def _mark_failed(connection: Connection, job_ids: Sequence[str]) -> None:
    """Mark ``failed``, in the caller's transaction, the documents of these jobs, which have used
    up their attempts. A job that a later job of the same stage replaced (a new version's input) is
    history: the later job decides, so its document keeps its state. A document in the trash stays
    there and comes back failed."""
    if not job_ids:
        return
    later = jobs.alias("later")
    needed = select(jobs.c.document_id).where(jobs.c.id.in_(set(job_ids)), ~_replaced(jobs, later))
    in_trash = documents.c.state == TRASHED
    connection.execute(
        update(documents)
        .where(documents.c.id.in_(needed))
        .values(
            # SET reads the row as it was, so both columns see the same state.
            state=case((in_trash, documents.c.state), else_=FAILED),
            state_before_trash=case((in_trash, FAILED), else_=None),
        )
    )


class JobQueue:
    """The queue over ``engine``. ``clock`` returns the current naive UTC time (tests set it)."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._engine = engine
        self._clock = clock

    def enqueue(
        self,
        document_id: str,
        stage: str,
        input_hash: str,
        *,
        priority: int = 0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        connection: Connection | None = None,
    ) -> str:
        """Queue ``stage`` for the document and return the job's id. If the same document, stage
        and input are already queued or running, nothing is added and that job's id is returned.

        With ``connection`` the job is written in the caller's transaction, so it exists exactly
        when the rows that need it do (the import writes the document and its first job together).
        """
        _check_stages((stage,))
        if not SHA256.fullmatch(input_hash):
            msg = "input_hash is a lower-case SHA-256"
            raise ValueError(msg)
        if max_attempts < 1:
            msg = "max_attempts is at least 1"
            raise ValueError(msg)
        now = self._clock()
        statement = (
            sqlite_insert(jobs)
            .values(
                id=ids.new_ulid(),
                document_id=document_id,
                stage=stage,
                status="queued",
                priority=priority,
                attempts=0,
                max_attempts=max_attempts,
                run_after=now,
                input_hash=input_hash,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["document_id", "stage", "input_hash"],
                index_where=jobs.c.status.in_(_OPEN),
            )
        )
        existing = select(jobs.c.id).where(
            jobs.c.document_id == document_id,
            jobs.c.stage == stage,
            jobs.c.input_hash == input_hash,
            jobs.c.status.in_(_OPEN),
        )
        if connection is not None:
            connection.execute(statement)
            job_id: str = connection.execute(existing).scalar_one()
            return job_id
        with self._engine.begin() as own:
            own.execute(statement)
            job_id = own.execute(existing).scalar_one()
        return job_id

    def claim(self, worker_id: str, stages: Iterable[str]) -> Job | None:
        """Give the worker the most urgent ready job of these stages, or None if there is none.

        Most urgent is the highest priority, then the one that has been ready longest, then the
        oldest. The attempt is counted here, so a job whose worker dies still uses up an attempt.
        """
        _check_worker(worker_id)
        chosen = _check_stages(stages)
        now = self._clock()
        candidate = (
            select(jobs.c.id)
            .where(jobs.c.status == "queued", jobs.c.stage.in_(chosen), jobs.c.run_after <= now)
            .order_by(jobs.c.priority.desc(), jobs.c.run_after, jobs.c.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(jobs)
            .where(jobs.c.id == candidate, jobs.c.status == "queued")
            .values(
                status="running",
                locked_by=worker_id,
                locked_at=now,
                attempts=jobs.c.attempts + 1,
            )
            .returning(
                jobs.c.id,
                jobs.c.document_id,
                jobs.c.stage,
                jobs.c.input_hash,
                jobs.c.attempts,
                jobs.c.max_attempts,
            )
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).first()
        if row is None:
            return None
        return Job(
            id=row.id,
            worker_id=worker_id,
            document_id=row.document_id,
            stage=row.stage,
            input_hash=row.input_hash,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
        )

    @staticmethod
    def _held(job_id: str, worker_id: str) -> ColumnElement[bool]:
        return and_(jobs.c.id == job_id, jobs.c.status == "running", jobs.c.locked_by == worker_id)

    def complete(
        self, job_id: str, worker_id: str, *, connection: Connection | None = None
    ) -> None:
        """Mark the worker's job done. With ``connection`` it is written in the caller's
        transaction, which a handler uses to commit its result and the job's end together
        (``Completion``); ``JobLostError`` raised there rolls the result back with it."""
        _check_worker(worker_id)
        statement = (
            update(jobs)
            .where(self._held(job_id, worker_id))
            .values(status="done", locked_by=None, locked_at=None, finished_at=self._clock())
        )
        if connection is not None:
            result = connection.execute(statement)
        else:
            with self._engine.begin() as own:
                result = own.execute(statement)
        if result.rowcount != 1:
            msg = "the worker no longer holds this job"
            raise JobLostError(msg)

    def release(self, job_id: str, worker_id: str) -> None:
        """Give back a job the worker claimed but never started: queued again as it was, without
        the attempt ``claim`` counted and without waiting for a back-off."""
        _check_worker(worker_id)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(jobs)
                .where(self._held(job_id, worker_id))
                .values(
                    status="queued",
                    locked_by=None,
                    locked_at=None,
                    attempts=jobs.c.attempts - 1,
                )
            )
        if result.rowcount != 1:
            msg = "the worker no longer holds this job"
            raise JobLostError(msg)

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: BaseException,
        *,
        connection: Connection | None = None,
    ) -> str:
        """Record that the worker's attempt failed with ``error``. Returns the job's new status:
        ``queued`` (it runs again after ``backoff``) or ``failed`` (no attempts left). With
        ``connection`` it is written in the caller's transaction (``Completion``)."""
        _check_worker(worker_id)
        if connection is not None:
            return self._fail(connection, job_id, worker_id, error)
        with self._engine.begin() as own:
            return self._fail(own, job_id, worker_id, error)

    def _fail(
        self, connection: Connection, job_id: str, worker_id: str, error: BaseException
    ) -> str:
        now = self._clock()
        name = error_name(error)
        row = connection.execute(
            select(jobs.c.document_id, jobs.c.attempts, jobs.c.max_attempts).where(
                self._held(job_id, worker_id)
            )
        ).first()
        if row is None:
            msg = "the worker no longer holds this job"
            raise JobLostError(msg)
        ended = row.attempts >= row.max_attempts
        values: dict[str, object] = {"locked_by": None, "locked_at": None, "last_error": name}
        if ended:
            values |= {"status": "failed", "finished_at": now}
        else:
            values |= {"status": "queued", "run_after": now + backoff(row.attempts)}
        result = connection.execute(
            update(jobs).where(self._held(job_id, worker_id)).values(**values)
        )
        if result.rowcount != 1:  # taken back between the two statements
            msg = "the worker no longer holds this job"
            raise JobLostError(msg)
        if ended:
            _mark_failed(connection, [job_id])
        return "failed" if ended else "queued"

    def retry(self, job_id: str) -> bool:
        """Queue a failed job again with all its attempts. False if it is not failed, or if the
        same work has been queued again since (then that job runs instead)."""
        now = self._clock()
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id, jobs.c.status == "failed")
                    .values(status="queued", attempts=0, run_after=now, finished_at=None)
                )
        except IntegrityError:
            return False
        return result.rowcount == 1

    def retry_document(self, document_id: str) -> int | RetryRefusal:
        """Queue again, with all their attempts, the document's failed jobs that are the latest
        job of their stage, and return how many. A failed job that a later job of the same stage
        replaced (a new version's input, or the same work queued again) is history and stays as it
        is. A document whose state is ``failed`` goes back to ``processing`` when anything was
        queued. Refused for an unknown id and for a document in the trash.

        One transaction that holds the write lock from its first statement decides and writes, so
        a move to the trash, a job that fails or a second press at the same moment comes wholly
        before or after it: pressing twice queues the work once.
        """
        if not ids.is_ulid(document_id):
            return RetryRefusal.NOT_FOUND
        now = self._clock()
        later = jobs.alias("later")
        with write_transaction(self._engine) as connection:
            state = connection.execute(
                select(documents.c.state).where(documents.c.id == document_id)
            ).scalar_one_or_none()
            if state is None:
                return RetryRefusal.NOT_FOUND
            if state == TRASHED:
                return RetryRefusal.IN_TRASH
            queued = connection.execute(
                update(jobs)
                .where(
                    jobs.c.document_id == document_id,
                    jobs.c.status == "failed",
                    ~_replaced(jobs, later),
                    # Never two open jobs for the same work (the database refuses it too).
                    ~exists().where(
                        later.c.document_id == jobs.c.document_id,
                        later.c.stage == jobs.c.stage,
                        later.c.input_hash == jobs.c.input_hash,
                        later.c.status.in_(_OPEN),
                    ),
                )
                .values(status="queued", attempts=0, run_after=now, finished_at=None)
            ).rowcount
            if queued and state == "failed":
                connection.execute(
                    update(documents)
                    .where(documents.c.id == document_id)
                    .values(state="processing")
                )
        return queued

    def recover(self) -> Recovery:
        """Take back every job left running by a process that ended. Call it at start-up, before
        any worker runs: a job that some live worker holds would be taken from it too.

        A job still ``running`` stored nothing (a handler commits its result with the job's
        ``done``, ``Completion``), so it is run again; on its last attempt it is failed instead,
        since the attempt counted at its claim is what stops a job that ends the process every
        time from running for ever."""
        now = self._clock()
        with self._engine.begin() as connection:
            ended = connection.execute(
                update(jobs)
                .where(jobs.c.status == "running", jobs.c.attempts >= jobs.c.max_attempts)
                .values(
                    status="failed",
                    locked_by=None,
                    locked_at=None,
                    last_error=INTERRUPTED,
                    finished_at=now,
                )
                .returning(jobs.c.id)
            ).all()
            failed = len(ended)
            _mark_failed(connection, [job.id for job in ended])
            requeued = connection.execute(
                update(jobs)
                .where(jobs.c.status == "running")
                .values(
                    status="queued",
                    locked_by=None,
                    locked_at=None,
                    last_error=INTERRUPTED,
                    run_after=now,
                )
            ).rowcount
        return Recovery(requeued=requeued, failed=failed)

    def counts(self) -> dict[tuple[str, str], int]:
        """How many jobs each (stage, status) has, for the status page."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(jobs.c.stage, jobs.c.status, func.count()).group_by(
                    jobs.c.stage, jobs.c.status
                )
            ).all()
        return {(row[0], row[1]): row[2] for row in rows}
