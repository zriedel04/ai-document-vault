"""The chunk stage: stored pages become chunks and index rows, and the document is searchable."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select, update

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
from vault.jobs.queue import JobQueue
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


def test_pages_whose_headings_change_while_they_are_chunked_are_not_stored(
    library: Library, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same text, other headings: every chunk still matches its page's text, so only comparing
    # the pages themselves shows that the section paths would come from headings that are gone.
    document_id, version_id, sha256 = imported(library)
    changed = [
        PageText(
            page.number, page.text, (PageHeading(1, "Renamed", 0, 18),) if page.headings else ()
        )
        for page in course_pages()
    ]
    chunking_with(
        monkeypatch, lambda: replace_pages(engine, version_id, changed, "text-1", "utf-8")
    )
    with pytest.raises(PagesChangedError) as raised:
        chunk_document(engine, document_id, sha256)
    assert CANARY not in str(raised.value)
    assert stored_chunks(engine, version_id) == []
    assert state(engine, document_id) == ("processing", None)
    monkeypatch.undo()
    assert chunk_document(engine, document_id, sha256) is Outcome.STORED
    assert any('"Renamed"' in row[5] for row in stored_chunks(engine, version_id))


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
        handlers={CHUNK_STAGE: chunk_stage.handler(engine)},
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
