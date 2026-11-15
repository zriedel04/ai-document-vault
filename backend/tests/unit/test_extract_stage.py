"""The extraction stage: a version's blob becomes its pages in the sandbox, then the chunk stage.

The extractors run in real sandbox children; a stand-in runner is used only for the failures a
real extractor cannot be made to cause on demand (a crash, the memory cap) and for answers a
hostile child could write.
"""

from __future__ import annotations

import functools
import io
import logging
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import docx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, insert, select, update

from tests.conftest import LOCAL
from tests.unit.pdf_builder import build, text_stream
from tests.unit.test_documents_api import Library
from vault.core.config import ProcessingSettings, Settings
from vault.db import ids
from vault.db.engine import read_snapshot
from vault.db.pages import HEADING_LEVELS, PageHeading, PageText, read_pages
from vault.db.schema import blobs, document_versions, documents, jobs
from vault.ingest import extract_stage
from vault.ingest.chunk_stage import CHUNK_STAGE
from vault.ingest.extract_stage import (
    DOCUMENT_FAILURES,
    EXTRACT_STAGE,
    EXTRACTORS,
    SETUP_DELAY,
    SETUP_FAILURES,
    BlobChangedError,
    ExtractionResultError,
    Outcome,
    VersionChangedError,
    extract_document,
)
from vault.ingest.extractors import Extraction, Heading, Page
from vault.ingest.extractors import docx as docx_extractor
from vault.ingest.extractors import pdf as pdf_extractor
from vault.ingest.importer import ImportRequest
from vault.ingest.importer import Outcome as ImportOutcome
from vault.ingest.validate import DEFAULT_LIMITS, EXTENSIONS
from vault.jobs import sandbox
from vault.jobs.queue import Completion, Job, JobQueue, utc_now
from vault.jobs.sandbox import (
    DEFAULT_MAX_RESULT_BYTES,
    DEFAULT_MAX_RESULT_MEMORY_BYTES,
    DEFAULT_MAX_RESULT_VALUES,
    MIB,
    SandboxCrashError,
    SandboxError,
    SandboxLimits,
    SandboxMemoryLimitError,
)
from vault.jobs.worker import WorkerPool
from vault.main import create_app
from vault.storage.blobs import BlobStore

CANARY = "Patient diagnosis confidential Jane Roe"
PRIVATE_NAME = "Jane Roe diagnosis.txt"
LIMITS = SandboxLimits(timeout_seconds=60, memory_bytes=512 * MIB)
WORKER = ids.new_ulid()


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


@pytest.fixture
def engine(app: FastAPI) -> Engine:
    result: Engine = app.state.engine
    return result


@pytest.fixture
def store(app: FastAPI) -> BlobStore:
    result: BlobStore = app.state.blobs
    return result


def add(library: Library, name: str, data: bytes) -> tuple[str, str]:
    """Import ``data`` as ``name``: the document's id and the SHA-256 of its bytes."""
    library.now += timedelta(minutes=1)
    path = library.folder / f"incoming-{ids.new_ulid()}{Path(name).suffix}"
    path.write_bytes(data)
    result = library.importer.import_file(
        ImportRequest(path=path, original_filename=name, method="upload", batch_id=ids.new_ulid())
    )
    assert result.outcome is ImportOutcome.IMPORTED
    assert result.document_id is not None
    with library.engine.connect() as connection:
        sha256: str = connection.execute(
            select(document_versions.c.blob_sha256).where(
                document_versions.c.document_id == result.document_id
            )
        ).scalar_one()
    return result.document_id, sha256


def claimed(engine: Engine, document_id: str) -> tuple[JobQueue, Job]:
    queue = JobQueue(engine)
    job = queue.claim(WORKER, [EXTRACT_STAGE])
    assert job is not None
    assert job.document_id == document_id
    return queue, job


def version_of(engine: Engine, document_id: str) -> str:
    with engine.connect() as connection:
        value: str = connection.execute(
            select(documents.c.current_version_id).where(documents.c.id == document_id)
        ).scalar_one()
    return value


def jobs_of(engine: Engine, document_id: str) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(jobs).where(jobs.c.document_id == document_id).order_by(jobs.c.created_at)
            )
        )


def state(engine: Engine, document_id: str) -> str:
    with engine.connect() as connection:
        value: str = connection.execute(
            select(documents.c.state).where(documents.c.id == document_id)
        ).scalar_one()
    return value


def docx_bytes() -> bytes:
    document = docx.Document()
    document.add_heading("Week 1", level=1)
    document.add_paragraph("Read chapter one.")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


# --- the extractors in the sandbox ---------------------------------------------------------------


def test_a_text_file_becomes_its_page_and_queues_the_chunk_job(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, sha256 = add(library, "notes.txt", b"Week 1: introduction\r\nWeek 2\n")
    queue, job = claimed(engine, document_id)
    assert extract_stage.handler(engine, queue, store, LIMITS)(job) is Completion.RECORDED
    stored = read_pages(engine, version_of(engine, document_id))
    assert stored is not None
    assert (stored.extractor, stored.encoding) == ("text-1", "utf-8")
    assert stored.pages == (PageText(1, "Week 1: introduction\nWeek 2\n"),)
    [extract, chunk] = jobs_of(engine, document_id)
    assert (extract.stage, extract.status) == (EXTRACT_STAGE, "done")
    assert (chunk.stage, chunk.status, chunk.input_hash) == (CHUNK_STAGE, "queued", sha256)
    assert state(engine, document_id) == "processing"  # searchable once chunked


def test_markdown_keeps_its_headings(library: Library, engine: Engine, store: BlobStore) -> None:
    document_id, _ = add(library, "course.md", b"# Course\n\nIntro\n\n## Week 1\nRead\n")
    queue, job = claimed(engine, document_id)
    extract_stage.handler(engine, queue, store, LIMITS)(job)
    stored = read_pages(engine, version_of(engine, document_id))
    assert stored is not None
    assert stored.pages[0].headings == (
        PageHeading(1, "Course", 0, 8),
        PageHeading(2, "Week 1", 17, 26),
    )


def test_the_configured_fallback_code_page_reaches_the_extractor(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, _ = add(library, "notes.txt", "Привет, мир\n".encode("cp1251"))
    queue, job = claimed(engine, document_id)
    extract_stage.handler(engine, queue, store, LIMITS, "cp1251")(job)
    stored = read_pages(engine, version_of(engine, document_id))
    assert stored is not None
    assert (stored.encoding, stored.pages[0].text) == ("cp1251", "Привет, мир\n")


def test_a_docx_file_is_extracted_too(library: Library, engine: Engine, store: BlobStore) -> None:
    document_id, _ = add(library, "week.docx", docx_bytes())
    queue, job = claimed(engine, document_id)
    extract_stage.handler(engine, queue, store, LIMITS)(job)
    stored = read_pages(engine, version_of(engine, document_id))
    assert stored is not None
    assert (stored.extractor, stored.encoding) == ("docx-1", None)
    assert stored.pages == (
        PageText(1, "Week 1\n\nRead chapter one.", (PageHeading(1, "Week 1", 0, 6),)),
    )


def test_docx_headings_at_every_built_in_level_are_stored(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    # Heading 7 to 9 are valid DOCX styles; storage must accept every level the extractor emits.
    document = docx.Document()
    for level in range(1, 10):
        document.add_heading(f"Level {level}", level=level)
    out = io.BytesIO()
    document.save(out)
    document_id, _ = add(library, "levels.docx", out.getvalue())
    queue, job = claimed(engine, document_id)
    assert extract_stage.handler(engine, queue, store, LIMITS)(job) is Completion.RECORDED
    stored = read_pages(engine, version_of(engine, document_id))
    assert stored is not None
    assert [h.level for h in stored.pages[0].headings] == list(range(1, 10))


def test_a_pdf_file_is_extracted_with_its_empty_pages_and_queues_the_chunk_job(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    data = build([text_stream(["Week 1", "Read chapter one."]), None, text_stream(["   "])])
    document_id, sha256 = add(library, "week.pdf", data)
    queue, job = claimed(engine, document_id)
    assert extract_stage.handler(engine, queue, store, LIMITS)(job) is Completion.RECORDED
    stored = read_pages(engine, version_of(engine, document_id))
    assert stored is not None
    assert (stored.extractor, stored.encoding) == ("pdf-1", None)
    # A page with no text layer, or only white space, is an empty page (the OCR stage's mark).
    assert stored.pages == (
        PageText(1, "Week 1\nRead chapter one."),
        PageText(2, ""),
        PageText(3, ""),
    )
    [_extract, chunk] = jobs_of(engine, document_id)
    assert (chunk.stage, chunk.status, chunk.input_hash) == (CHUNK_STAGE, "queued", sha256)


def test_a_pdf_with_no_text_layer_at_all_is_stored_as_empty_pages(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, _ = add(library, "scan.pdf", build([None, None]))
    queue, job = claimed(engine, document_id)
    assert extract_stage.handler(engine, queue, store, LIMITS)(job) is Completion.RECORDED
    stored = read_pages(engine, version_of(engine, document_id))
    assert stored is not None
    assert stored.pages == (PageText(1, ""), PageText(2, ""))


def test_the_stage_runs_the_pdf_extractor_with_the_sandboxs_default_answer_caps() -> None:
    # The PDF extractor's caps were chosen so that its largest answer fits these caps
    # (test_pdf_extractor); the stage's limits come from the settings and must keep them.
    limits = SandboxLimits.from_settings(ProcessingSettings())
    assert limits.max_result_bytes >= DEFAULT_MAX_RESULT_BYTES
    assert limits.max_result_memory_bytes >= DEFAULT_MAX_RESULT_MEMORY_BYTES
    assert limits.max_result_values >= DEFAULT_MAX_RESULT_VALUES


def test_the_largest_pdfs_the_extractor_takes_run_within_the_default_limits() -> None:
    # The product's default timeout and memory cap must suit a PDF at the extractor's caps: every
    # page it takes with the text cap nearly spent, and a file of the largest size it takes (text
    # pages, then filler after the end of the file, which PDFium reads past).
    limits = SandboxLimits.from_settings(ProcessingSettings())
    caps = DEFAULT_LIMITS
    line = "x" * 70
    per_page = caps.max_pdf_text_chars // caps.max_pdf_pages // (len(line) + 1)
    many = build([text_stream([line] * per_page)] * caps.max_pdf_pages)
    started = time.monotonic()
    result = sandbox.run("pdf", many, limits)
    assert isinstance(result, Extraction)
    assert len(result.pages) == caps.max_pdf_pages
    assert sum(len(page.text) for page in result.pages) <= caps.max_pdf_text_chars
    small = build([text_stream(["Week 1"])])
    large = small + b"%" * (caps.max_bytes - len(small))
    assert sandbox.run("pdf", large, limits) == Extraction("pdf-1", (Page(1, "Week 1"),))
    del large
    # A tenth of the timeout, so a slower machine still has room.
    assert time.monotonic() - started < limits.timeout_seconds / 10


def test_every_heading_level_an_extractor_can_emit_can_be_stored() -> None:
    docx_levels = set(docx_extractor._HEADING_LEVELS.values())
    markdown_levels = set(range(1, 7))  # `#` to `######`, the CommonMark range (ADR 0018)
    assert docx_levels | markdown_levels <= set(HEADING_LEVELS)


def test_each_extractor_is_an_entry_point_with_the_version_its_extractor_writes() -> None:
    assert EXTRACTORS["docx"].version == docx_extractor.VERSION
    assert EXTRACTORS["pdf"].version == pdf_extractor.VERSION
    assert sandbox.ENTRY_POINTS["pdf"].target == "vault.ingest.extractors.pdf:extract"
    for extension, extractor in EXTRACTORS.items():
        assert extension in EXTENSIONS
        point = sandbox.ENTRY_POINTS[extractor.entry]
        assert bool(point.options) is extractor.text


def test_the_server_and_the_evaluation_load_no_extractor_library() -> None:
    # The extractors run in the sandbox only: the server (and the evaluation, which runs the stage)
    # must never load PDFium or python-docx, which parse the file in the child.
    code = (
        "import sys, vault.main, vault.eval;"
        "print(sorted(m for m in ('pypdfium2', 'docx') if m in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603 - a fixed command
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


# --- stale work -----------------------------------------------------------------------------------


def move_to_other_bytes(engine: Engine, document_id: str) -> None:
    """Give the document a new current version holding other bytes."""
    other = "ef" * 32
    version_id = ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(
            insert(blobs).values(
                sha256=other,
                size=1,
                ext="txt",
                mime="text/plain",
                created_at=library_time(connection),
            )
        )
        connection.execute(
            insert(document_versions).values(
                id=version_id,
                document_id=document_id,
                blob_sha256=other,
                version_no=2,
                origin="import",
                created_at=library_time(connection),
            )
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(current_version_id=version_id)
        )


def library_time(connection: Connection) -> Any:
    return connection.execute(select(blobs.c.created_at)).scalars().first()


def test_a_document_that_moved_to_other_bytes_during_the_run_stores_nothing(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, _ = add(library, "notes.txt", f"{CANARY}\n".encode())
    old_version = version_of(engine, document_id)
    queue, job = claimed(engine, document_id)

    def moving(*args: Any, **kwargs: Any) -> object:
        result = sandbox.run(*args, **kwargs)
        move_to_other_bytes(engine, document_id)  # commits while the child ran
        return result

    handler = extract_stage.handler(engine, queue, store, LIMITS, runner=moving)
    assert handler(job) is Completion.RECORDED
    assert read_pages(engine, old_version) is None
    assert [(row.stage, row.status) for row in jobs_of(engine, document_id)] == [
        (EXTRACT_STAGE, "done")
    ]


def test_without_a_job_stale_work_runs_nothing(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, _ = add(library, "notes.txt", b"text\n")

    def never(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError

    for target, sha256 in [(document_id, "ef" * 32), ("not-a-ulid", "ef" * 32)]:
        outcome = extract_document(engine, store, target, sha256, limits=LIMITS, runner=never)
        assert outcome is Outcome.STALE


@contextmanager
def empty_snapshot(_engine: Engine) -> Iterator[Any]:
    class Nothing:
        def execute(self, _statement: object) -> Any:
            return self

        def first(self) -> None:
            return None

    yield Nothing()


def test_a_version_that_comes_back_after_the_snapshot_is_extracted_again(
    library: Library, engine: Engine, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, sha256 = add(library, "notes.txt", b"text\n")
    snapshots = [empty_snapshot, read_snapshot]
    monkeypatch.setattr(extract_stage, "read_snapshot", lambda e: snapshots.pop(0)(e))
    outcome = extract_document(engine, store, document_id, sha256, limits=LIMITS)
    assert outcome is Outcome.STORED
    assert read_pages(engine, version_of(engine, document_id)) is not None


def test_a_version_that_keeps_coming_back_fails_the_attempt(
    library: Library, engine: Engine, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, sha256 = add(library, "notes.txt", b"text\n")
    monkeypatch.setattr(extract_stage, "read_snapshot", empty_snapshot)
    with pytest.raises(VersionChangedError):
        extract_document(engine, store, document_id, sha256, limits=LIMITS)
    queue, job = claimed(engine, document_id)
    assert extract_stage.handler(engine, queue, store, LIMITS)(job) is Completion.ATTEMPT_QUEUED
    assert jobs_of(engine, document_id)[0].last_error == "VersionChangedError"


# --- failures ------------------------------------------------------------------------------------


def test_every_sandbox_failure_is_either_the_documents_or_the_setups() -> None:
    def subclasses(cls: type) -> set[type]:
        found: set[type] = set(cls.__subclasses__())
        return found.union(*(subclasses(sub) for sub in found))

    assert set(DOCUMENT_FAILURES) | set(SETUP_FAILURES) == subclasses(SandboxError)
    assert not set(DOCUMENT_FAILURES) & set(SETUP_FAILURES)


def run_in_pool(engine: Engine, store: BlobStore, document_id: str, **options: Any) -> Any:
    """Run the document's extract job once through a worker pool; its row afterwards."""
    queue = JobQueue(engine)
    pool = WorkerPool(
        queue,
        {EXTRACT_STAGE: extract_stage.handler(engine, queue, store, **options)},
        {EXTRACT_STAGE: 1},
        extensions={EXTRACT_STAGE: extract_stage.EXTENSIONS},
        idle_seconds=0.05,
    )
    pool.start()
    try:
        deadline = time.monotonic() + 60
        while jobs_of(engine, document_id)[0].last_error is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        pool.stop(30)
    return jobs_of(engine, document_id)[0]


def binary_after_the_head() -> bytes:
    """Text in the head the validator reads, and NUL bytes after it."""
    return (f"{CANARY}\n" * 400).encode()[:8192] + b"\x00" * 64


def crash(*_args: Any, **_kwargs: Any) -> object:
    raise SandboxCrashError(-6)


def out_of_memory(*_args: Any, **_kwargs: Any) -> object:
    raise SandboxMemoryLimitError


FAILURES: dict[str, tuple[bytes, dict[str, Any]]] = {
    "SandboxTimeoutError": (b"text\n", {"limits": SandboxLimits(0.001, 512 * MIB)}),
    "SandboxResultError": (
        b"text\n",
        {"limits": SandboxLimits(60, 512 * MIB, max_result_bytes=8)},
    ),
    "SandboxChildError": (binary_after_the_head(), {"limits": LIMITS}),
    "SandboxCrashError": (b"text\n", {"limits": LIMITS, "runner": crash}),
    "SandboxMemoryLimitError": (b"text\n", {"limits": LIMITS, "runner": out_of_memory}),
}


@pytest.mark.parametrize("error", sorted(FAILURES))
def test_a_failure_the_document_can_cause_uses_an_attempt_and_is_retried(
    library: Library,
    engine: Engine,
    store: BlobStore,
    error: str,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert {cls.__name__ for cls in DOCUMENT_FAILURES} == set(FAILURES)
    data, options = FAILURES[error]
    caplog.set_level(logging.DEBUG)
    document_id, _ = add(library, PRIVATE_NAME, data)
    row = run_in_pool(engine, store, document_id, **options)
    assert (row.status, row.attempts, row.last_error) == ("queued", 1, error)
    assert state(engine, document_id) == "processing"
    assert read_pages(engine, version_of(engine, document_id)) is None
    output = capfd.readouterr()
    for private in (CANARY, PRIVATE_NAME, "NotTextError"):
        assert private not in caplog.text + output.out + output.err


def test_the_last_failed_attempt_fails_the_document(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, _ = add(library, "notes.txt", binary_after_the_head())
    with engine.begin() as connection:
        connection.execute(update(jobs).values(max_attempts=1))
    row = run_in_pool(engine, store, document_id, limits=LIMITS)
    assert (row.status, row.last_error) == ("failed", "SandboxChildError")
    assert state(engine, document_id) == "failed"


# PDFs the extractor refuses, each holding the canary: the attempt fails as an extractor that
# raised, and neither the refusal's class nor the text reaches the job, the log or the console.
HOSTILE_PDFS = [
    pytest.param(build([text_stream([CANARY])], user_password=b"pw"), id="needs a password"),
    pytest.param(
        build([text_stream([CANARY])], user_password=b"", security_filter="Unknown"),
        id="unknown handler",
    ),
    pytest.param(build([]), id="no pages"),
    pytest.param(build([text_stream([CANARY])], claimed_pages=3), id="a page it does not hold"),
    pytest.param(b"%PDF-1.7\n" + CANARY.encode() * 50 + b"\n%%EOF\n", id="corrupt"),
    pytest.param(build([text_stream([CANARY])])[:300], id="cut short"),
]


@pytest.mark.parametrize("data", HOSTILE_PDFS)
def test_a_pdf_the_extractor_refuses_fails_the_attempt_by_class_name_only(
    library: Library,
    engine: Engine,
    store: BlobStore,
    data: bytes,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    document_id, _ = add(library, "Jane Roe diagnosis.pdf", data)
    row = run_in_pool(engine, store, document_id, limits=LIMITS)
    assert (row.status, row.attempts, row.last_error) == ("queued", 1, "SandboxChildError")
    assert read_pages(engine, version_of(engine, document_id)) is None
    output = capfd.readouterr()
    refusals = ("EncryptedPdfError", "EmptyPdfError", "NotPdfError", "PdfLimitError")
    for private in (CANARY, "Jane Roe", *refusals):
        assert private not in caplog.text + output.out + output.err + str(row.last_error)


@pytest.mark.parametrize(
    "answer",
    [
        Extraction(
            "pdf-1", (Page(1, "Week 1", (Heading(1, "Week 1", 0, 6),)),)
        ),  # pdf-1 finds none
        Extraction("pdf-1", (Page(1, "Week 1"),), "utf-8"),  # a PDF has no encoding
        Extraction("pdf-2", (Page(1, "Week 1"),)),
        Extraction("docx-1", (Page(1, "Week 1"),)),
    ],
)
def test_a_pdf_answer_the_extractor_cannot_give_is_refused(
    library: Library, engine: Engine, store: BlobStore, answer: object
) -> None:
    document_id, sha256 = add(library, "week.pdf", build([text_stream(["Week 1"])]))
    with pytest.raises(ExtractionResultError):
        extract_document(
            engine, store, document_id, sha256, limits=LIMITS, runner=lambda *_a, **_k: answer
        )
    assert read_pages(engine, version_of(engine, document_id)) is None


def unsupported_system(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(sandbox, "SUPPORTED_PLATFORMS", frozenset())
    return {}


def refused(_monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return {"runner": functools.partial(sandbox.run, entry_points={})}


@pytest.mark.parametrize(
    ("setup", "error"),
    [(refused, "SandboxRefusedError"), (unsupported_system, "SandboxUnavailableError")],
)
def test_a_failure_of_the_setup_defers_the_job_without_an_attempt(
    library: Library,
    engine: Engine,
    store: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[pytest.MonkeyPatch], dict[str, Any]],
    error: str,
) -> None:
    document_id, _ = add(library, "notes.txt", b"text\n")
    with engine.begin() as connection:
        connection.execute(update(jobs).values(max_attempts=1))  # deferring never uses it up
    before = utc_now()
    row = run_in_pool(engine, store, document_id, limits=LIMITS, **setup(monkeypatch))
    assert (row.status, row.attempts, row.last_error) == ("queued", 0, error)
    assert row.run_after >= before + SETUP_DELAY
    assert state(engine, document_id) == "processing"


def test_a_type_with_no_extractor_is_deferred_if_it_is_ever_claimed(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, _ = add(library, "notes.txt", b"text\n")
    with engine.begin() as connection:
        connection.execute(update(blobs).values(ext="png"))
    queue, job = claimed(engine, document_id)  # claimed without the stage's extensions
    assert extract_stage.handler(engine, queue, store, LIMITS)(job) is Completion.DEFERRED
    [row] = jobs_of(engine, document_id)
    assert (row.status, row.attempts, row.last_error) == ("queued", 0, "NoExtractorError")
    assert (
        JobQueue(engine).claim(WORKER, [EXTRACT_STAGE], extensions=extract_stage.EXTENSIONS) is None
    )


def test_a_blob_that_no_longer_holds_its_bytes_is_never_extracted(
    library: Library, engine: Engine, store: BlobStore
) -> None:
    document_id, sha256 = add(library, "notes.txt", b"original\n")
    path = store.find(sha256)
    assert path is not None
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    path.write_bytes(b"replaced\n")  # the same size, other bytes

    def never(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError

    with pytest.raises(BlobChangedError):
        extract_document(engine, store, document_id, sha256, limits=LIMITS, runner=never)
    path.write_bytes(b"original\n and more")  # the right start, but longer
    with pytest.raises(BlobChangedError):
        extract_document(engine, store, document_id, sha256, limits=LIMITS, runner=never)


TEXT_PAGE = Page(1, "Week 1\n", (Heading(1, "Week 1", 0, 6),))


@pytest.mark.parametrize(
    "answer",
    [
        b"not an extraction",
        Extraction("docx-1", (TEXT_PAGE,), "utf-8"),  # another extractor's name
        Extraction("text-2", (TEXT_PAGE,), "utf-8"),
        Extraction("text-1", (TEXT_PAGE,), "cp1251"),  # not the fallback it was given
        Extraction("text-1", (TEXT_PAGE,), "ascii"),
        Extraction("text-1", (TEXT_PAGE,), None),
        Extraction("text-1", (), "utf-8"),  # no page
        Extraction("text-1", (Page(2, "x"),), "utf-8"),  # not numbered from 1
        Extraction("text-1", (Page(1, "x", (Heading(1, "x", 0, 9),)),), "utf-8"),
        Extraction("text-1", (Page(1, "x", (Heading(10, "x", 0, 1),)),), "utf-8"),
        Extraction("text-1", (Page(1, "a\x00b"),), "utf-8"),
    ],
)
def test_an_answer_the_extractor_cannot_give_is_refused(
    library: Library, engine: Engine, store: BlobStore, answer: object
) -> None:
    document_id, sha256 = add(library, "notes.txt", b"Week 1\n")
    with pytest.raises(ExtractionResultError):
        extract_document(
            engine, store, document_id, sha256, limits=LIMITS, runner=lambda *_a, **_k: answer
        )
    assert read_pages(engine, version_of(engine, document_id)) is None


@pytest.mark.parametrize("fallback", ["utf-8", "latin-1", "CP1252"])
def test_a_fallback_that_is_not_a_windows_code_page_is_refused_before_any_job(
    engine: Engine, store: BlobStore, fallback: str
) -> None:
    with pytest.raises(ValueError, match="code page"):
        extract_stage.handler(engine, JobQueue(engine), store, LIMITS, fallback)
    with pytest.raises(ValueError, match="code page"):
        extract_document(engine, store, ids.new_ulid(), "ab" * 32, limits=LIMITS, fallback=fallback)


def test_every_encoding_the_text_extractor_reports_is_accepted() -> None:
    accepted = EXTRACTORS["txt"].encodings("cp1361")
    assert accepted == {"utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "johab"}
    assert EXTRACTORS["docx"].encodings("cp1252") == {None}


# --- the app ------------------------------------------------------------------------------------


def upload(client: TestClient, name: str, body: bytes) -> str:
    response = client.post(
        "/api/imports/upload", content=body, headers={"X-Vault-Filename": quote(name, safe="")}
    )
    assert response.status_code == 200, response.text
    document_id: str = response.json()["document_id"]
    return document_id


def test_imported_text_and_markdown_become_searchable(
    vault_dir: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = {
        "vault": {"path": str(vault_dir)},
        "processing": {"text_fallback_encoding": "cp1251"},
    }
    app = create_app(Settings.model_validate(settings))
    engine: Engine = app.state.engine
    headers = {"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    with TestClient(app, base_url=LOCAL, headers=headers) as client:
        notes = upload(client, PRIVATE_NAME, "Зачёт xylophone\r\n".encode("cp1251"))
        course = upload(client, "Jane Roe course.md", b"# Syllabus\n\nquokka seminar\n")
        deadline = time.monotonic() + 60
        while {state(engine, notes), state(engine, course)} != {"searchable"}:
            assert time.monotonic() < deadline, "the documents never became searchable"
            time.sleep(0.05)
        found = {}
        for query in ("xylophone", "quokka", "Зачёт"):
            response = client.post("/api/search", json={"query": query})
            assert response.status_code == 200, response.text
            found[query] = [item["document_id"] for item in response.json()["items"]]
    assert found == {"xylophone": [notes], "quokka": [course], "Зачёт": [notes]}
    output = capfd.readouterr()
    for private in ("xylophone", "quokka", "Jane Roe", "Syllabus"):
        assert private not in caplog.text + output.out + output.err


def test_imported_pdfs_become_searchable_scans_included(
    vault_dir: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    app = create_app(Settings.model_validate({"vault": {"path": str(vault_dir)}}))
    engine: Engine = app.state.engine
    headers = {"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    text_pdf = build([text_stream(["Syllabus"]), None, text_stream(["quokka seminar"])])
    with TestClient(app, base_url=LOCAL, headers=headers) as client:
        course = upload(client, "Jane Roe course.pdf", text_pdf)
        scan = upload(client, "Jane Roe scan.pdf", build([None]))
        deadline = time.monotonic() + 60
        while {state(engine, course), state(engine, scan)} != {"searchable"}:
            assert time.monotonic() < deadline, "the documents never became searchable"
            time.sleep(0.05)
        response = client.post("/api/search", json={"query": "quokka"})
        assert response.status_code == 200, response.text
        [item] = response.json()["items"]
        pages = client.get(f"/api/documents/{scan}/pages")
        assert pages.status_code == 200, pages.text
    assert (item["document_id"], item["page"]) == (course, 3)
    assert [page["text"] for page in pages.json()["pages"]] == [""]
    output = capfd.readouterr()
    for private in ("quokka", "Jane Roe", "Syllabus"):
        assert private not in caplog.text + output.out + output.err
