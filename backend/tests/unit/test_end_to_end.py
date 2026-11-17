"""Phase 2 end to end: upload, the real worker pool and sandbox, then keyword search.

The plan's Phase 2 is done when "a keyword search over your sample set returns the right documents
with snippets, and clicking one opens the right page", and its testing asks for chunk traceability
(every chunk maps to a valid page and offsets) and phrase and code-term tests ("CSE 485").

A small synthetic set (TXT, Markdown, DOCX and a three-page PDF) is uploaded through
``POST /api/imports/upload`` to an app started with its lifespan, so the app's own worker pool runs
the extract, metadata and chunk stages, extraction and file properties in the sandbox, until every
document is ``searchable``. Nothing is stubbed. One app, one upload and one pipeline run serve
every check here (a module fixture), which keeps the run to a few sandboxed child processes.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import docx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from vault.core.config import Settings
from vault.db.schema import chunks, document_versions, documents, jobs, pages
from vault.main import create_app
from vault.storage import acl

from ..conftest import LOCAL
from . import pdf_builder
from .test_chunk_stage import wait_for
from .test_search import find, marked
from .test_upload import send

# Long enough for several chunks with overlap (the chunker packs 400-600 tokens).
FILLER = " ".join(
    f"Paragraph {number} reviews the weekly reading on query plans and index design."
    for number in range(60)
)


@dataclass(frozen=True)
class Sample:
    name: str
    data: bytes
    phrase: str  # a phrase only this document holds, on ``page``
    letters: str  # a course code only this document holds, on ``page``
    digits: str
    page: int
    # Whether the document writes the code apart (``CSE 485``, ``MAT-343``) or joined (``PHY121``).
    joined: bool = False


def docx_bytes() -> bytes:
    document = docx.Document()
    document.add_heading("Lab handbook", level=1)
    document.add_paragraph("Safety goggles stay on during every titration.")
    document.add_heading("Course", level=2)
    document.add_paragraph("Welcome to PHY121, where the pendulum lab opens the term.")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def pdf_bytes() -> bytes:
    return pdf_builder.build(
        [
            pdf_builder.text_stream(["Annual report", "The cover page lists no course."]),
            pdf_builder.text_stream(["Budget review", "Quarterly freight volumes rose sharply."]),
            pdf_builder.text_stream(["Seminar", "ECO 210 meets in the north hall on Fridays."]),
        ]
    )


SAMPLES = (
    Sample(
        name="syllabus.txt",
        data=(f"Course outline\n\n{FILLER}\n\nCSE 485 covers boundary value analysis.\n").encode(),
        phrase="boundary value analysis",
        letters="CSE",
        digits="485",
        page=1,
    ),
    Sample(
        name="notes.md",
        data=(
            b"# Linear algebra\n\nIntro text.\n\n## Exam\n\n"
            b"MAT-343 asks for eigenvalues of symmetric matrices.\n"
        ),
        phrase="eigenvalues of symmetric matrices",
        letters="MAT",
        digits="343",
        page=1,
    ),
    Sample(
        name="handbook.docx",
        data=docx_bytes(),
        phrase="pendulum lab opens the term",
        letters="PHY",
        digits="121",
        page=1,
        joined=True,
    ),
    Sample(
        name="report.pdf",
        data=pdf_bytes(),
        phrase="meets in the north hall",
        letters="ECO",
        digits="210",
        page=3,
    ),
)
# A phrase on another page of the PDF, so a hit's page is checked against more than one page.
PDF_PAGE_2 = "freight volumes rose"


def code_queries(sample: Sample) -> list[str]:
    """The ways the plan's query prep writes a code (section 8: CSE 485 / CSE485), in upper and
    lower case, and with a hyphen where the document writes the code apart."""
    letters, digits = sample.letters, sample.digits
    queries = [f"{letters} {digits}", f"{letters}{digits}", f"{letters.lower()}{digits}"]
    if not sample.joined:
        # A hyphen is not a token, so ``cse-485`` is the phrase ``cse 485``: it finds a code
        # written apart, and not one written joined (``PHY121`` is one token).
        queries.append(f"{letters.lower()}-{digits}")
    return queries


@dataclass
class Run:
    client: TestClient
    engine: Engine
    ids: dict[str, str]  # sample name -> document id


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    # The autouse fixture that skips PowerShell is function-scoped; this module runs one app.
    patch = pytest.MonkeyPatch()
    patch.setattr(acl, "IS_WINDOWS", False)
    vault = tmp_path_factory.mktemp("end-to-end") / "vault"
    app: FastAPI = create_app(Settings.model_validate({"vault": {"path": str(vault)}}))
    engine: Engine = app.state.engine
    headers = {"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    try:
        with TestClient(app, base_url=LOCAL, headers=headers) as client:  # starts the pool
            ids: dict[str, str] = {}
            for sample in SAMPLES:
                response = send(client, sample.data, name=sample.name)
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["outcome"] == "imported", body
                ids[sample.name] = body["document_id"]
            wait_for(lambda: finished(engine, set(ids.values())), seconds=180)
            yield Run(client, engine, ids)
    finally:
        patch.undo()


def finished(engine: Engine, document_ids: set[str]) -> bool:
    """Every document searchable and every one of its jobs done; a failed job ends the wait."""
    with engine.connect() as connection:
        states = dict(
            connection.execute(
                select(documents.c.id, documents.c.state).where(documents.c.id.in_(document_ids))
            ).all()
        )
        statuses = connection.execute(
            select(jobs.c.stage, jobs.c.status, jobs.c.last_error).where(
                jobs.c.document_id.in_(document_ids)
            )
        ).all()
    assert not [row for row in statuses if row.status == "failed"], statuses
    return all(state == "searchable" for state in states.values()) and all(
        row.status == "done" for row in statuses
    )


def stored_pages(engine: Engine, document_id: str) -> dict[int, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(pages.c.page_no, pages.c.text)
            .join(document_versions, document_versions.c.id == pages.c.version_id)
            .join(documents, documents.c.current_version_id == document_versions.c.id)
            .where(documents.c.id == document_id)
        ).all()
    return {row.page_no: row.text for row in rows}


def stored_chunks(engine: Engine, document_id: str) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(chunks)
                .join(documents, documents.c.current_version_id == chunks.c.version_id)
                .where(documents.c.id == document_id)
                .order_by(chunks.c.chunk_index)
            ).all()
        )


def squeezed(value: str) -> str:
    """Letters and digits only, in lower case: how a code reads whatever separates its parts."""
    return re.sub(r"[\W_]+", "", value).lower()


# --- the pipeline -----------------------------------------------------------------------------


def test_every_sample_went_through_every_stage(run: Run) -> None:
    with run.engine.connect() as connection:
        stages = connection.execute(
            select(jobs.c.document_id, jobs.c.stage).where(jobs.c.document_id.in_(run.ids.values()))
        ).all()
    seen = {(row.document_id, row.stage) for row in stages}
    for sample in SAMPLES:
        has_reader = sample.name.endswith((".docx", ".pdf"))  # metadata_stage.READERS
        expected = {"extract", "chunk"} | ({"metadata"} if has_reader else set())
        assert {stage for document, stage in seen if document == run.ids[sample.name]} == expected
    assert len(stored_pages(run.engine, run.ids["report.pdf"])) == 3
    assert len(stored_chunks(run.engine, run.ids["syllabus.txt"])) > 1  # overlap is exercised


# --- chunk traceability ------------------------------------------------------------------------


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample.name)
def test_every_chunk_is_its_span_of_a_stored_page(run: Run, sample: Sample) -> None:
    document_id = run.ids[sample.name]
    texts = stored_pages(run.engine, document_id)
    assert sorted(texts) == list(range(1, len(texts) + 1))
    rows = stored_chunks(run.engine, document_id)
    assert rows, "a document with text has chunks"
    assert [row.chunk_index for row in rows] == list(range(len(rows)))
    for row in rows:
        assert row.document_id == document_id
        assert row.page_start in texts
        assert row.page_end in texts
        if row.page_start == row.page_end:
            assert 0 <= row.char_start < row.char_end <= len(texts[row.page_start])
            assert texts[row.page_start][row.char_start : row.char_end] == row.text
        else:  # the schema allows it; the span runs from one page into a later one
            assert row.page_start < row.page_end
            assert row.text.startswith(texts[row.page_start][row.char_start :])
            assert row.text.endswith(texts[row.page_end][: row.char_end])
    # Every page with text is covered by a chunk.
    covered = {page for row in rows for page in range(row.page_start, row.page_end + 1)}
    assert {page for page, text in texts.items() if text.strip()} <= covered


# --- search --------------------------------------------------------------------------------------


def check_hit(run: Run, hit: dict[str, Any], document_id: str, page: int, words: str) -> None:
    """The hit is the document's, at ``page``; its span is a chunk of that page that holds
    ``words``; its snippet marks them; and the pages API opens that page with them on it."""
    assert hit["document_id"] == document_id
    assert hit["page"] == page
    [chunk] = [row for row in stored_chunks(run.engine, document_id) if row.id == hit["chunk_id"]]
    assert (chunk.page_start, chunk.char_start, chunk.char_end) == (
        page,
        hit["char_start"],
        hit["char_end"],
    )
    texts = stored_pages(run.engine, document_id)
    assert squeezed(words) in squeezed(texts[page][hit["char_start"] : hit["char_end"]])
    # The passage is on that page and on no other.
    assert [number for number, text in texts.items() if squeezed(words) in squeezed(text)] == [page]
    snippet = "".join(part["text"] for part in hit["snippet"])
    assert squeezed(words) in squeezed(snippet)
    assert squeezed(words) in squeezed("".join(marked(hit)))
    # "Clicking one opens the right page": the page the hit names, read through the API.
    response = run.client.get(
        f"/api/documents/{document_id}/pages", params={"offset": page - 1, "limit": 1}
    )
    assert response.status_code == 200, response.text
    [opened] = response.json()["pages"]
    assert opened["number"] == page
    assert opened["text"] == texts[page]


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample.name)
def test_a_phrase_finds_its_document_at_its_page(run: Run, sample: Sample) -> None:
    body = find(run.client, f'"{sample.phrase}"')
    assert body["total"] == 1
    [hit] = body["items"]
    check_hit(run, hit, run.ids[sample.name], sample.page, sample.phrase)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample.name)
def test_a_code_finds_its_document_however_it_is_written(run: Run, sample: Sample) -> None:
    for query in code_queries(sample):
        body = find(run.client, query)
        assert body["total"] == 1, query
        [hit] = body["items"]
        check_hit(run, hit, run.ids[sample.name], sample.page, sample.letters + sample.digits)


def test_a_phrase_on_another_page_of_the_pdf_points_at_that_page(run: Run) -> None:
    [hit] = find(run.client, f'"{PDF_PAGE_2}"')["items"]
    check_hit(run, hit, run.ids["report.pdf"], 2, PDF_PAGE_2)


def test_a_phrase_is_not_found_with_its_words_out_of_order(run: Run) -> None:
    assert find(run.client, '"symmetric eigenvalues"')["total"] == 0
    assert find(run.client, "symmetric eigenvalues")["total"] == 1
