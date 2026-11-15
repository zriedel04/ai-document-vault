"""Which sandbox failures are the document's and which are the setup's, for every stage that runs
the sandbox (``vault.ingest.extract_stage``, ``vault.ingest.metadata_stage``).

ADR 0005: a failed job marks its document Failed once its attempts are used up. That is right for a
failure the document can cause, and wrong for one that says the sandbox cannot run at all on this
system, which no document can fix and every document would hit. So the sandbox's errors are split
in two, here and only here, so that the stages cannot disagree:

- ``DOCUMENT_FAILURES`` (a timeout, the memory cap, a crash, the child raising, an answer that
  cannot be read): the stage lets the error fail the attempt, by class name only, and the queue
  retries it within the job's attempts;
- ``SETUP_FAILURES`` (an entry point the allowlist refuses, a child that cannot be started or
  limited on this system): ``run_deferring_setup`` gives the job back without using an attempt
  (``JobQueue.defer``) and it runs again after ``SETUP_DELAY``, its error's class name showing why
  it waits. The document is never failed by it.

Every ``SandboxError`` subclass is in exactly one of the two (a test checks it), so a new one must
be placed before it can ship.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Final

from vault.jobs.queue import Completion, Job, JobQueue
from vault.jobs.sandbox import (
    SandboxChildError,
    SandboxCrashError,
    SandboxMemoryLimitError,
    SandboxRefusedError,
    SandboxResultError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)

# How long a job waits after a failure of the setup before it is tried again.
SETUP_DELAY: Final = timedelta(minutes=10)

# The sandbox failures a document can cause: the attempt fails and is retried within its attempts.
DOCUMENT_FAILURES: Final = (
    SandboxTimeoutError,
    SandboxMemoryLimitError,
    SandboxCrashError,
    SandboxChildError,
    SandboxResultError,
)
# The sandbox failures of the setup: the job is deferred, the document is never failed by them.
SETUP_FAILURES: Final = (SandboxRefusedError, SandboxUnavailableError)


def run_deferring_setup(
    queue: JobQueue,
    job: Job,
    work: Callable[[], Completion],
    also: tuple[type[Exception], ...] = (),
) -> Completion:
    """Run ``work`` for the worker's ``job``; if it raises one of ``SETUP_FAILURES``, or one of
    ``also`` (a stage's own failures of the setup), defer the job by ``SETUP_DELAY`` without
    using an attempt and return ``Completion.DEFERRED``. Every other error propagates, so the
    worker records it as a failed attempt."""
    setup: tuple[type[Exception], ...] = (*SETUP_FAILURES, *also)
    try:
        return work()
    except setup as error:
        queue.defer(job.id, job.worker_id, error, SETUP_DELAY)
        return Completion.DEFERRED
