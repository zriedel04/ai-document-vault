"""Background processing: whether the worker pool takes new jobs, how many jobs each stage has,
and pausing and resuming it (plan section 7, ADR 0005).

A pause stops the pool from claiming new jobs; jobs already running finish, and the answer comes
only once no claim is in progress, so no job starts after it. A resume lets the pool claim again.
The pause lasts until a resume or until the server stops: the pool starts unpaused at every start.
Both run in a worker thread, because a pause waits for claims in progress, which can wait on
SQLite's busy timeout, and that must not hold the event loop.

A pause or resume answers with the flag alone (``ProcessingState``) and reads nothing after the
change: an answer that also read the counts could fail after the pool had changed, and an error
would then report as not done what was done. So an error from either means the pool was not
changed, and the counts come from ``GET /api/processing``.

The answers hold a flag and counts only. Nothing here logs; the pool logs its own pause and
resume as fixed events.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import Engine

from vault.api.documents import Stage
from vault.core.security import LocalUser, current_user
from vault.db.schema import JOB_STAGES
from vault.jobs.queue import JobQueue
from vault.jobs.worker import WorkerPool

router = APIRouter(tags=["processing"])


class StageJobs(BaseModel):
    """How many jobs of one stage are in each status."""

    stage: Stage
    queued: int
    running: int
    done: int
    failed: int


class ProcessingState(BaseModel):
    """Whether the pool takes new jobs, as a pause or resume left it."""

    paused: bool


class ProcessingStatus(BaseModel):
    paused: bool
    # Every stage, in the order of the pipeline, with zeros for a stage that has no jobs.
    stages: list[StageJobs]


def _vault(request: Request) -> tuple[Engine, WorkerPool]:
    engine: Engine | None = request.app.state.engine
    workers: WorkerPool | None = request.app.state.workers
    if engine is None or workers is None:
        raise HTTPException(status_code=503, detail="no vault is open")
    return engine, workers


def _status(engine: Engine, workers: WorkerPool) -> ProcessingStatus:
    counts = JobQueue(engine).counts()
    return ProcessingStatus(
        paused=workers.paused,
        stages=[
            StageJobs(
                stage=stage,
                queued=counts.get((stage, "queued"), 0),
                running=counts.get((stage, "running"), 0),
                done=counts.get((stage, "done"), 0),
                failed=counts.get((stage, "failed"), 0),
            )
            for stage in JOB_STAGES
        ],
    )


@router.get("/processing")
async def read_processing(
    request: Request, _user: Annotated[LocalUser, Depends(current_user)]
) -> ProcessingStatus:
    """Whether processing is paused, and how many jobs each stage has in each status."""
    engine, workers = _vault(request)
    return await run_in_threadpool(_status, engine, workers)


@router.post("/processing/pause")
async def pause_processing(
    request: Request, _user: Annotated[LocalUser, Depends(current_user)]
) -> ProcessingState:
    """Stop taking new jobs. Running jobs finish; once this answers, no new job starts until a
    resume or a restart of the server. Pausing twice is the same as once."""
    _engine, workers = _vault(request)
    await run_in_threadpool(workers.pause)
    return ProcessingState(paused=workers.paused)


@router.post("/processing/resume")
async def resume_processing(
    request: Request, _user: Annotated[LocalUser, Depends(current_user)]
) -> ProcessingState:
    """Take new jobs again. Resuming when not paused changes nothing."""
    _engine, workers = _vault(request)
    await run_in_threadpool(workers.resume)
    return ProcessingState(paused=workers.paused)
