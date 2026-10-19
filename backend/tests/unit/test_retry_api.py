"""Retrying a document's failed processing: ``POST /api/documents/{id}/retry``."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from vault.core.config import Settings
from vault.db import ids
from vault.db.schema import documents, jobs
from vault.jobs.queue import JobQueue
from vault.main import create_app

from ..conftest import LOCAL
from .test_documents_api import URL, Library, library, set_state

__all__ = ["library"]  # the fixture, imported so pytest finds it here

WORKER = ids.new_ulid()
NAME = "Patient-Name diagnosis.txt"


class Workers:
    """Stands in for the worker pool and counts the wake-ups."""

    def __init__(self) -> None:
        self.woken = 0

    def wake(self) -> None:
        self.woken += 1


@pytest.fixture
def workers(app: FastAPI) -> Workers:
    stand_in = Workers()
    app.state.workers = stand_in
    return stand_in


def fail_extraction(app: FastAPI, document_id: str) -> str:
    """Use up the attempts of the document's extraction job, as a worker would."""
    queue = JobQueue(app.state.engine)
    with app.state.engine.connect() as connection:
        job_id: str = connection.execute(
            select(jobs.c.id).where(jobs.c.document_id == document_id)
        ).scalar_one()
        attempts = connection.execute(
            select(jobs.c.max_attempts).where(jobs.c.id == job_id)
        ).scalar_one()
    for _ in range(attempts):
        claimed = queue.claim(WORKER, ["extract"])
        assert claimed is not None
        assert claimed.id == job_id
        queue.fail(job_id, WORKER, RuntimeError())
        with app.state.engine.begin() as connection:  # skip the back-off
            connection.execute(
                jobs.update().where(jobs.c.id == job_id).values(run_after=jobs.c.created_at)
            )
    return job_id


def test_retry_queues_the_failed_job_and_wakes_the_workers(
    client: TestClient, library: Library, app: FastAPI, workers: Workers
) -> None:
    document_id = library.add(NAME, "Rent is due on the first.\n")
    fail_extraction(app, document_id)
    set_state(library, document_id, "failed")
    before = client.get(f"{URL}/{document_id}").json()
    assert before["jobs"][0]["status"] == "failed"

    response = client.post(f"{URL}/{document_id}/retry")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "processing"
    (job,) = body["jobs"]
    assert (job["status"], job["attempts"], job["finished_at"]) == ("queued", 0, None)
    assert job["last_error"] == "RuntimeError"
    assert body == client.get(f"{URL}/{document_id}").json()
    assert workers.woken == 1


def test_retry_with_nothing_failed_returns_the_document_unchanged(
    client: TestClient, library: Library, workers: Workers
) -> None:
    document_id = library.add(NAME, "Rent is due on the first.\n")
    before = client.get(f"{URL}/{document_id}").json()
    response = client.post(f"{URL}/{document_id}/retry")
    assert response.status_code == 200
    assert response.json() == before
    assert workers.woken == 0


def test_pressing_retry_twice_queues_the_work_once(
    client: TestClient, library: Library, app: FastAPI, workers: Workers
) -> None:
    document_id = library.add(NAME, "Rent is due on the first.\n")
    fail_extraction(app, document_id)
    assert client.post(f"{URL}/{document_id}/retry").status_code == 200
    assert client.post(f"{URL}/{document_id}/retry").status_code == 200
    assert workers.woken == 1
    with app.state.engine.connect() as connection:
        statuses = connection.execute(
            select(jobs.c.status).where(jobs.c.document_id == document_id)
        ).scalars()
        assert list(statuses) == ["queued"]


def test_a_document_in_the_trash_is_refused(
    client: TestClient, library: Library, app: FastAPI, workers: Workers
) -> None:
    document_id = library.add(NAME, "Rent is due on the first.\n")
    job_id = fail_extraction(app, document_id)
    library.trash(document_id)
    response = client.post(f"{URL}/{document_id}/retry")
    assert response.status_code == 409
    assert response.json() == {"detail": "the document is in the trash"}
    assert workers.woken == 0
    with app.state.engine.connect() as connection:
        assert (
            connection.execute(select(jobs.c.status).where(jobs.c.id == job_id)).scalar_one()
            == "failed"
        )
        assert (
            connection.execute(
                select(documents.c.state).where(documents.c.id == document_id)
            ).scalar_one()
            == "trashed"
        )


@pytest.mark.parametrize(
    "document_id", ["01J9ZZZZZZZZZZZZZZZZZZZZZZ", "Patient-Name", "a" * 26, "..%2Fvault.db"]
)
def test_retrying_an_unknown_document_is_a_404_that_does_not_repeat_the_id(
    client: TestClient, library: Library, document_id: str
) -> None:
    library.add(NAME, "Rent is due on the first.\n")
    response = client.post(f"{URL}/{document_id}/retry")
    assert response.status_code == 404
    assert "Patient" not in response.text
    assert "vault" not in response.text


def test_retry_logs_a_count_and_nothing_about_the_document(
    client: TestClient,
    library: Library,
    app: FastAPI,
    workers: Workers,
    caplog: pytest.LogCaptureFixture,
) -> None:
    document_id = library.add(NAME, "Patient-Name canary text.\n")
    fail_extraction(app, document_id)
    with caplog.at_level(logging.DEBUG):
        assert client.post(f"{URL}/{document_id}/retry").status_code == 200
    # The application's own records; the formatter drops third-party messages (vault.core.logging).
    logged = " ".join(
        f"{record.getMessage()} {record.__dict__}"
        for record in caplog.records
        if record.name.startswith("vault.")
    )
    assert "documents.retried" in logged
    assert document_id not in logged
    assert "Patient" not in logged


def test_retry_without_a_worker_pool_still_queues(
    client: TestClient, library: Library, app: FastAPI
) -> None:
    app.state.workers = None
    document_id = library.add(NAME, "Rent is due on the first.\n")
    fail_extraction(app, document_id)
    response = client.post(f"{URL}/{document_id}/retry")
    assert response.status_code == 200
    assert response.json()["jobs"][0]["status"] == "queued"


def test_retry_needs_the_token(anonymous: TestClient) -> None:
    # With the Origin a page sends, so it is the token that is missing (no Origin is a 403).
    response = anonymous.post(f"{URL}/{ids.new_ulid()}/retry", headers={"Origin": LOCAL})
    assert response.status_code == 401


def test_retry_refuses_another_origin(
    app: FastAPI, token: str, library: Library, workers: Workers
) -> None:
    document_id = library.add(NAME, "Rent is due on the first.\n")
    fail_extraction(app, document_id)
    other = TestClient(
        app,
        base_url=LOCAL,
        headers={"Authorization": f"Bearer {token}", "Origin": "http://evil.example"},
    )
    assert other.post(f"{URL}/{document_id}/retry").status_code == 403
    assert workers.woken == 0


def test_without_a_vault_there_is_nothing_to_retry() -> None:
    app = create_app(Settings())
    client = TestClient(
        app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    )
    assert client.post(f"{URL}/{ids.new_ulid()}/retry").status_code == 503


def test_jobs_are_listed_in_queue_order_so_the_page_sees_the_job_a_retry_would_queue(
    app: FastAPI, client: TestClient, library: Library, workers: Workers
) -> None:
    # Two ULIDs made in the same millisecond sort at random, so an older job of the same step can
    # have the higher id. Retry takes the latest job by queue time; the list must agree with it.
    document_id = library.add(NAME, "body")
    failed = fail_extraction(app, document_id)
    with app.state.engine.begin() as connection:
        created = connection.execute(
            select(jobs.c.created_at).where(jobs.c.id == failed)
        ).scalar_one()
        older = ids.new_ulid(now_ms=int(time.time() * 1000) + 60_000)  # a higher id
        assert older > failed
        connection.execute(
            jobs.insert().values(
                id=older,
                document_id=document_id,
                stage="extract",
                status="done",
                priority=0,
                attempts=1,
                max_attempts=3,
                run_after=created - timedelta(seconds=1),
                input_hash="0" * 64,
                created_at=created - timedelta(seconds=1),
                finished_at=created,
            )
        )
    listed = client.get(f"{URL}/{document_id}").json()["jobs"]
    assert [job["status"] for job in listed] == ["done", "failed"]
    assert client.post(f"{URL}/{document_id}/retry").status_code == 200
    with app.state.engine.connect() as connection:
        assert connection.execute(
            select(jobs.c.status).where(jobs.c.id == failed)
        ).scalar_one() == ("queued")
