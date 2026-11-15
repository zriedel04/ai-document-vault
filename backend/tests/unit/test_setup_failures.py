"""The one rule for which sandbox failures are the document's and which are the setup's, and the
ways the real sandbox fails its setup, which the stage tests run their handlers against."""

from __future__ import annotations

import functools
import subprocess
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import pytest

from vault.ingest import extract_stage, metadata_stage
from vault.ingest.setup_failures import (
    DOCUMENT_FAILURES,
    SETUP_DELAY,
    SETUP_FAILURES,
    run_deferring_setup,
)
from vault.jobs import sandbox
from vault.jobs.queue import Completion, Job, JobQueue
from vault.jobs.sandbox import (
    SandboxChildError,
    SandboxError,
    SandboxRefusedError,
    SandboxUnavailableError,
)


def refused(_monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An entry point the allowlist does not name."""
    return {"runner": functools.partial(sandbox.run, entry_points={})}


def unsupported_system(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(sandbox, "SUPPORTED_PLATFORMS", frozenset())
    return {}


def child_cannot_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError(2, "not found")

    monkeypatch.setattr(subprocess, "Popen", broken)
    return {}


def child_cannot_be_limited(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(sandbox._Guard, "attach", lambda _self, _process: False)
    return {}


# Each way the real sandbox can fail its setup, with the error class it raises: the stage tests
# apply one (it returns the handler options it needs) after their file is imported.
type Setup = Callable[[pytest.MonkeyPatch], dict[str, Any]]
SETUP_KINDS: dict[str, tuple[Setup, str]] = {
    "refused": (refused, "SandboxRefusedError"),
    "unsupported_system": (unsupported_system, "SandboxUnavailableError"),
    "child_cannot_start": (child_cannot_start, "SandboxUnavailableError"),
    "child_cannot_be_limited": (child_cannot_be_limited, "SandboxUnavailableError"),
}


def test_every_sandbox_failure_is_either_the_documents_or_the_setups() -> None:
    def subclasses(cls: type) -> set[type]:
        found = set(cls.__subclasses__())
        return found.union(*(subclasses(sub) for sub in found))

    assert set(DOCUMENT_FAILURES) | set(SETUP_FAILURES) == subclasses(SandboxError)
    assert not set(DOCUMENT_FAILURES) & set(SETUP_FAILURES)
    assert {error for _setup, error in SETUP_KINDS.values()} == {
        cls.__name__ for cls in SETUP_FAILURES
    }


def test_no_stage_keeps_its_own_copy_of_the_rule() -> None:
    for stage in (extract_stage, metadata_stage):
        for name in ("SETUP_FAILURES", "DOCUMENT_FAILURES", "SETUP_DELAY"):
            assert not hasattr(stage, name), (stage.__name__, name)


class _Queue:
    """Records what ``run_deferring_setup`` asks of the queue."""

    def __init__(self) -> None:
        self.deferred: list[tuple[str, str, str, timedelta]] = []

    def defer(self, job_id: str, worker_id: str, error: BaseException, delay: timedelta) -> None:
        self.deferred.append((job_id, worker_id, type(error).__name__, delay))


JOB = Job(
    id="job",
    worker_id="worker",
    document_id="document",
    stage="metadata",
    input_hash="0" * 64,
    attempts=1,
    max_attempts=3,
)


def raising(error: Exception) -> Callable[[], Completion]:
    def work() -> Completion:
        raise error

    return work


@pytest.mark.parametrize("error", [SandboxRefusedError(), SandboxUnavailableError()])
def test_a_setup_failure_defers_the_job(error: Exception) -> None:
    queue = _Queue()
    assert run_deferring_setup(cast("JobQueue", queue), JOB, raising(error)) is Completion.DEFERRED
    assert queue.deferred == [("job", "worker", type(error).__name__, SETUP_DELAY)]


def test_a_stage_can_add_its_own_setup_failures() -> None:
    class StageSetupError(Exception):
        pass

    queue = _Queue()
    work = raising(StageSetupError())
    assert (
        run_deferring_setup(cast("JobQueue", queue), JOB, work, also=(StageSetupError,))
        is Completion.DEFERRED
    )
    assert queue.deferred == [("job", "worker", "StageSetupError", SETUP_DELAY)]


@pytest.mark.parametrize("error", [SandboxChildError("NotDocxError"), ValueError()])
def test_any_other_failure_is_left_to_the_worker(error: Exception) -> None:
    queue = _Queue()
    with pytest.raises(type(error)):
        run_deferring_setup(cast("JobQueue", queue), JOB, raising(error))
    assert queue.deferred == []
    assert run_deferring_setup(cast("JobQueue", queue), JOB, lambda: Completion.RECORDED) is (
        Completion.RECORDED
    )
    assert queue.deferred == []
