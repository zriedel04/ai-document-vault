"""The processing endpoints: status, pause and resume of the worker pool."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import LOCAL
from tests.unit.test_documents_api import Library
from vault.core.config import Settings
from vault.db.schema import JOB_STAGES
from vault.jobs.queue import Job, JobQueue
from vault.jobs.worker import WorkerPool
from vault.main import create_app

URL = "/api/processing"


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


def counts(body: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        row["stage"]: {key: row[key] for key in ("queued", "running", "done", "failed")}
        for row in body["stages"]
    }


def test_the_status_lists_every_stage_in_order_with_its_counts(
    client: TestClient, library: Library
) -> None:
    library.add("One.txt", "first\n")
    library.add("Two.txt", "second\n")
    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["paused"] is False
    assert [row["stage"] for row in body["stages"]] == list(JOB_STAGES)
    table = counts(body)
    assert table["extract"] == {"queued": 2, "running": 0, "done": 0, "failed": 0}
    assert all(sum(table[stage].values()) == 0 for stage in JOB_STAGES if stage != "extract")


def test_pause_and_resume_change_the_flag_and_say_so(app: FastAPI, client: TestClient) -> None:
    pool: WorkerPool = app.state.workers
    paused = client.post(f"{URL}/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert pool.paused
    assert client.post(f"{URL}/pause").json()["paused"] is True  # twice is the same as once
    assert client.get(URL).json()["paused"] is True
    resumed = client.post(f"{URL}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert not pool.paused
    assert client.post(f"{URL}/resume").json()["paused"] is False


def test_a_paused_pool_starts_no_job_until_it_is_resumed(
    app: FastAPI, client: TestClient, library: Library
) -> None:
    ran: list[str] = []
    pool = WorkerPool(
        JobQueue(app.state.engine),
        handlers={"extract": lambda job: ran.append(job.document_id)},
        limits={"extract": 1},
        idle_seconds=0.05,
    )
    app.state.workers = pool
    assert client.post(f"{URL}/pause").json()["paused"] is True
    document_id = library.add("Lease.txt", "Rent is due on the first.\n")
    pool.start()
    try:
        pool.wake()
        time.sleep(0.5)
        assert ran == []
        assert counts(client.get(URL).json())["extract"]["queued"] == 1
        client.post(f"{URL}/resume")
        deadline = time.monotonic() + 20
        while counts(client.get(URL).json())["extract"]["done"] != 1:
            assert time.monotonic() < deadline, "the job did not run after the resume"
            time.sleep(0.02)
    finally:
        pool.stop(10)
    assert ran == [document_id]


@pytest.mark.parametrize(("action", "paused"), [("pause", True), ("resume", False)])
def test_a_pause_or_resume_answers_with_the_flag_and_reads_no_counts(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, action: str, paused: bool
) -> None:
    # The counts failing (a busy or broken database) must not turn a pause that took effect into
    # an error: the page would then believe nothing changed.
    def broken(self: JobQueue) -> dict[tuple[str, str], int]:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(JobQueue, "counts", broken)
    client = TestClient(
        app,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL},
        raise_server_exceptions=False,
    )
    if not paused:
        app.state.workers.pause()
    response = client.post(f"{URL}/{action}")
    assert response.status_code == 200
    assert response.json() == {"paused": paused}
    assert app.state.workers.paused is paused
    assert client.get(URL).status_code == 500  # the counts really fail


class SlowQueue(JobQueue):
    """A queue whose claims wait until ``go`` is set, like a claim held up by SQLite's busy
    timeout while another writer works."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.claiming = threading.Event()
        self.go = threading.Event()

    def claim(self, worker_id: str, stages: Iterable[str]) -> Job | None:
        self.claiming.set()
        self.go.wait(30)
        return super().claim(worker_id, stages)


def test_a_pause_waiting_for_a_claim_does_not_hold_up_other_requests(
    vault_dir: Path,
) -> None:
    app = create_app(Settings.model_validate({"vault": {"path": str(vault_dir)}}))
    queue = SlowQueue(app.state.engine)
    pool = WorkerPool(queue, handlers={"extract": lambda job: None}, limits={"extract": 1})
    app.state.workers = pool
    headers = {"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    # One client in a ``with`` block: every request runs on one event loop, as in the server.
    with TestClient(app, base_url=LOCAL, headers=headers) as client:
        pool.start()
        try:
            assert queue.claiming.wait(10)
            paused: dict[str, Any] = {}
            pausing = threading.Thread(
                target=lambda: paused.update(client.post(f"{URL}/pause").json())
            )
            pausing.start()
            time.sleep(0.2)  # the pause is now waiting for the claim
            assert pausing.is_alive()
            answered: list[int] = []
            reading = threading.Thread(target=lambda: answered.append(client.get(URL).status_code))
            reading.start()
            reading.join(5)
            assert answered == [200], "the event loop was held by the pause"
        finally:
            queue.go.set()
        pausing.join(10)
        assert paused["paused"] is True
        pool.stop(10)


@pytest.mark.parametrize(
    ("method", "path"), [("get", URL), ("post", f"{URL}/pause"), ("post", f"{URL}/resume")]
)
def test_processing_needs_the_token(anonymous: TestClient, method: str, path: str) -> None:
    # With the Origin a page sends, so it is the token that is missing (no Origin is a 403).
    response = anonymous.request(method, path, headers={"Origin": LOCAL})
    assert response.status_code == 401


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_pause_and_resume_refuse_another_origin(app: FastAPI, token: str, action: str) -> None:
    other = TestClient(
        app,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {token}", "Origin": "http://evil.example"},
    )
    assert other.post(f"{URL}/{action}").status_code == 403
    assert not app.state.workers.paused


@pytest.mark.parametrize(
    ("method", "path"), [("get", URL), ("post", f"{URL}/pause"), ("post", f"{URL}/resume")]
)
def test_without_a_vault_there_is_no_processing(method: str, path: str) -> None:
    app = create_app(Settings())
    client = TestClient(
        app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    )
    response = client.request(method, path)
    assert response.status_code == 503
    assert response.json() == {"detail": "no vault is open"}
