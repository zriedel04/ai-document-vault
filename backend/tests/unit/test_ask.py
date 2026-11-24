"""Ask the Vault end to end (``vault.answers.ask``) on a synthetic vault with offline providers.

Every document, title and question here is made up. The "private" ones carry canary words, so a
test can show that none of them reaches a log line, an error or a request it must not reach.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Generator, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, update

from tests.unit.test_documents_schema import (
    NOW,
    blob_row,
    document_row,
    source_row,
    version_row,
)
from vault.ai import egress
from vault.ai.egress import EgressDecision, EgressMode, Withheld
from vault.ai.providers.base import (
    ErrorKind,
    LLMError,
    LLMEvent,
    LLMRequest,
    LLMResponse,
    StopReason,
    Task,
    Usage,
    parse_structured,
)
from vault.ai.providers.fake import FakeProvider
from vault.ai.routing import PROVIDER_NAMES, ModelChoice
from vault.ai.usage import LedgerError
from vault.answers import ask as ask_module
from vault.answers.ask import (
    MAX_QUESTION_CHARS,
    MAX_QUESTION_TERMS,
    AskFailedError,
    AskFailure,
    AskResult,
    Outcome,
    ask,
)
from vault.answers.citations import FindingCode, SegmentStatus
from vault.core.config import Settings
from vault.core.logging import SafeFormatter
from vault.db import ids
from vault.db.chunks import replace_chunks
from vault.db.engine import create_vault_engine
from vault.db.labels import name_key
from vault.db.migrate import upgrade_to_latest
from vault.db.pages import PageHeading, PageText, replace_pages
from vault.db.schema import (
    ai_requests,
    blobs,
    chunks,
    document_labels,
    document_versions,
    documents,
    import_sources,
    labels,
)
from vault.ingest.chunking.structural_v1 import CHUNKER_VERSION, chunk_pages
from vault.retrieval.keyword import build_match

SYLLABUS_TITLE = "CSE485-Syllabus-Quillfeather.pdf"
SYLLABUS = (
    "The final exam for CSE485 is on December 10 in Room 201. Late work loses five points per day."
)
NOTES_TITLE = "Study-Notes-Marrowgate.txt"
NOTES = "Bring two pencils to the final exam. The study group meets on Tuesday."
PRIVATE_TITLE = "Patient-Intake-Canary-Vellichor.txt"
PRIVATE = (
    "Patient Jordan Canary-Vellichor reports a penicillin allergy and anxiety about the final "
    "exam on December 10."
)
SECTION = "Grading-Canary-Sonder"
QUESTION = "When is the final exam for CSE485, Canary-Petrichor?"
CANARIES = ("Quillfeather", "Marrowgate", "Vellichor", "Sonder", "Petrichor", "penicillin")

FAKE_ROUTES = {
    "answer": {"provider": "fake", "model": "fake-answer"},
    "answer_deep": {"provider": "fake", "model": "fake-deep"},
}
CLOUD_ROUTES = {
    "answer": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
    "answer_deep": {"provider": "anthropic", "model": "claude-opus-5-5"},
}
PRICES = {"input": Decimal(2), "cached_input": Decimal("0.2"), "output": Decimal(10)}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


def settings(routes: dict[str, Any] = FAKE_ROUTES, **retrieval: int) -> Settings:
    return Settings.model_validate(
        {
            "ai": {"routes": routes},
            "pricing": {
                "fake": {"fake-answer": PRICES, "fake-deep": PRICES},
                "anthropic": {"claude-sonnet-5-5": PRICES},
            },
            "retrieval": retrieval,
        }
    )


def add_document(
    engine: Engine,
    filename: str,
    *page_texts: str,
    local_only: bool = False,
    section: str | None = None,
) -> tuple[str, list[int]]:
    """A document with one version whose pages are ``page_texts``, chunked; its chunk ids."""
    sha = hashlib.sha256(ids.new_ulid().encode()).hexdigest()
    document = document_row(local_only=local_only)
    version = version_row(document["id"], blob_sha256=sha)
    with engine.begin() as connection:
        connection.execute(insert(blobs).values(blob_row(sha256=sha)))
        connection.execute(insert(documents).values(document))
        connection.execute(insert(document_versions).values(version))
        connection.execute(
            update(documents)
            .where(documents.c.id == document["id"])
            .values(current_version_id=version["id"])
        )
        connection.execute(
            insert(import_sources).values(
                source_row(document["id"], original_filename=filename, original_path=None)
            )
        )
    pages = []
    for number, body in enumerate(page_texts, start=1):
        headings = (
            (PageHeading(1, section, 0, len(section)),)
            if section is not None and number == 1
            else ()
        )
        text = f"{section}\n{body}" if section is not None and number == 1 else body
        pages.append(PageText(number, text, headings))
    replace_pages(engine, version["id"], pages, "text-1", "utf-8")
    replace_chunks(engine, version["id"], chunk_pages(pages), CHUNKER_VERSION)
    with engine.connect() as connection:
        chunk_ids = connection.execute(
            select(chunks.c.id).where(chunks.c.document_id == document["id"]).order_by(chunks.c.id)
        ).scalars()
        return document["id"], list(chunk_ids)


def add_local_only_label(engine: Engine, document_id: str) -> None:
    label_id = ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(
            insert(labels).values(
                id=label_id,
                kind="collection",
                name="Canary-Vellichor collection",
                name_key=name_key("Canary-Vellichor collection"),
                parent_id=None,
                local_only=True,
            )
        )
        connection.execute(
            insert(document_labels).values(
                document_id=document_id,
                label_id=label_id,
                source="user",
                status="accepted",
                created_at=NOW,
            )
        )


def ledger(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(select(ai_requests).order_by(ai_requests.c.id)))


class Spy:
    """A provider that records every request and answers with ``inner``, or with ``answer``."""

    def __init__(
        self,
        name: str = "fake",
        inner: FakeProvider | None = None,
        answer: dict[str, Any] | None = None,
        stop: StopReason = StopReason.COMPLETE,
        raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self.inner = inner or FakeProvider()
        self.answer = answer
        self.stop = stop
        self.raises = raises
        self.requests: list[LLMRequest] = []

    def generate(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if self.raises is not None:
            raise self.raises
        if self.answer is None:
            return self.inner.generate(req)
        text = json.dumps(self.answer)
        return LLMResponse(
            text=text,
            structured=parse_structured(text) if self.stop is StopReason.COMPLETE else None,
            usage=Usage(input=120, output=40, cached=20),
            stop_reason=self.stop,
            provider_request_id=None,
            latency_ms=7,
        )

    def stream(self, req: LLMRequest) -> Generator[LLMEvent, None, None]:
        return self.inner.stream(req)

    def count_tokens(self, req: LLMRequest) -> int:
        return self.inner.count_tokens(req)


def run(
    engine: Engine,
    spy: Spy,
    question: str = QUESTION,
    config: Settings | None = None,
    **options: Any,
) -> AskResult:
    return ask(
        engine,
        config or settings(),
        question,
        providers=lambda name: spy,
        **options,
    )


def sent_text(request: LLMRequest) -> str:
    return " ".join(
        [request.messages[-1].content]
        + [f"{source.title} {source.text}" for source in request.sources]
    )


# --- answering ---------------------------------------------------------------------------------


def test_a_question_is_answered_with_validated_citations_and_one_usage_row(engine: Engine) -> None:
    syllabus, syllabus_chunks = add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    notes, _ = add_document(engine, NOTES_TITLE, NOTES)
    spy = Spy()
    result = run(engine, spy)

    assert result.outcome is Outcome.ANSWERED
    assert (result.task, result.provider, result.model) == (Task.ANSWER, "fake", "fake-answer")
    assert result.prompt_id == "answer.v1"
    assert [source.label for source in result.sources] == ["S1", "S2"]
    assert {source.document_id for source in result.sources} == {syllabus, notes}
    assert all(source.cited for source in result.sources)
    assert result.validated is not None
    assert result.insufficient_evidence is False
    assert result.validated.findings == ()
    assert [s.status for s in result.validated.segments] == [SegmentStatus.SUPPORTED] * 2
    # The citation resolves to the server's ids, never to anything the model wrote.
    by_document = {source.document_id: source for source in result.sources}
    citation = next(
        c
        for segment in result.validated.segments
        for c in segment.citations
        if result.source(c.label) is by_document[syllabus]
    )
    assert citation.document_id == syllabus
    assert result.chunks_for(citation) == tuple(syllabus_chunks)
    # One request, holding the question and the two passages, labelled in order.
    (request,) = spy.requests
    assert request.task is Task.ANSWER
    assert request.model == "fake-answer"
    assert request.messages[-1].content == QUESTION
    assert [source.label for source in request.sources] == ["S1", "S2"]
    assert {source.title for source in request.sources} == {SYLLABUS_TITLE, NOTES_TITLE}
    # One usage row, for this call.
    (row,) = ledger(engine)
    assert result.ledger_id == row.id
    assert (row.task, row.provider, row.model, row.status) == (
        "answer",
        "fake",
        "fake-answer",
        "ok",
    )
    assert row.doc_count == 2
    assert row.est_cost_nanousd is not None
    assert (result.sources_found, result.sources_withheld, result.withheld) == (2, 0, ())


def test_a_deep_answer_uses_the_deep_route_and_is_recorded_as_that_task(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    spy = Spy()
    result = run(engine, spy, deep=True)
    assert (result.task, result.model) == (Task.ANSWER_DEEP, "fake-deep")
    assert spy.requests[0].task is Task.ANSWER_DEEP
    (row,) = ledger(engine)
    assert (row.task, row.model) == ("answer_deep", "fake-deep")


def test_the_pickers_choice_wins_over_the_route(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    spy = Spy()
    result = run(engine, spy, override=ModelChoice(provider="fake", model="fake-picked"))
    assert result.model == "fake-picked"
    assert spy.requests[0].model == "fake-picked"
    (row,) = ledger(engine)
    assert row.model == "fake-picked"
    assert row.est_cost_nanousd is None  # no price for that model: unknown, never zero


def test_a_source_of_two_chunks_resolves_a_quote_to_the_chunk_it_is_in(engine: Engine) -> None:
    second = "The second page lists the room for the makeup exam."
    _, chunk_ids = add_document(engine, SYLLABUS_TITLE, SYLLABUS, second)
    assert len(chunk_ids) == 2
    result = run(engine, Spy(), config=settings(context_neighbor_chunks=1))
    (source,) = result.sources
    assert source.chunk_ids == tuple(chunk_ids)
    assert (source.page_start, source.page_end) == (1, 2)
    assert result.validated is not None
    (segment,) = result.validated.segments
    (citation,) = segment.citations
    # The fake quotes the opening sentence, which is on the first page only.
    assert result.chunks_for(citation) == (chunk_ids[0],)


def test_an_invented_label_is_removed_and_marks_its_segment_unsupported(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    answer = {
        "answer": [
            {"text": "The exam is on December 10.", "sources": ["S1"], "quote": "December 10"},
            {"text": "It is in Room 201.", "sources": ["S1", "S7"], "quote": "Room 201"},
            {"text": "It lasts three hours.", "sources": ["[S1]"], "quote": "three hours"},
        ],
        "insufficient_evidence": False,
        "missing": None,
    }
    result = run(engine, Spy(answer=answer))
    assert result.validated is not None
    statuses = [segment.status for segment in result.validated.segments]
    assert statuses == [
        SegmentStatus.SUPPORTED,
        SegmentStatus.UNSUPPORTED,
        SegmentStatus.UNSUPPORTED,
    ]
    invented = [f for f in result.validated.findings if f.code is FindingCode.INVENTED_LABEL]
    assert [(f.segment, f.source) for f in invented] == [(1, 1), (2, 0)]
    # The given label of the unsupported segment is still resolved; the invented ones are not.
    assert [c.label for c in result.validated.segments[1].citations] == ["S1"]
    assert result.validated.segments[2].citations == ()
    (row,) = ledger(engine)
    assert (row.status, row.input_tokens, row.output_tokens, row.cached_tokens) == (
        "ok",
        120,
        40,
        20,
    )


def test_the_models_own_insufficient_evidence_flag_is_returned(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    answer = {"answer": [], "insufficient_evidence": True, "missing": "No source gives the time."}
    result = run(engine, Spy(answer=answer))
    assert result.outcome is Outcome.ANSWERED
    assert result.insufficient_evidence is True
    assert [source.cited for source in result.sources] == [False]
    assert len(ledger(engine)) == 1


# --- no passages, no call ----------------------------------------------------------------------


@pytest.mark.parametrize("question", ["Zeppelin hangar location?", "?!", "- & ...", "☃ ✓"])
def test_no_passage_means_no_call_and_no_usage_row(engine: Engine, question: str) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    spy = Spy()
    result = run(engine, spy, question=question)
    assert result.outcome is Outcome.NO_PASSAGES
    assert result.insufficient_evidence is True
    assert (result.validated, result.prompt_id, result.ledger_id) == (None, None, None)
    assert result.sources == ()
    assert spy.requests == []
    assert ledger(engine) == []


def test_an_empty_vault_gives_insufficient_evidence(engine: Engine) -> None:
    spy = Spy()
    result = run(engine, spy)
    assert result.outcome is Outcome.NO_PASSAGES
    assert spy.requests == []


def test_a_document_in_the_trash_is_not_retrieved(engine: Engine) -> None:
    document_id, _ = add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    with engine.begin() as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(state="trashed", trashed_at=NOW, state_before_trash="searchable")
        )
    spy = Spy()
    assert run(engine, spy).outcome is Outcome.NO_PASSAGES
    assert spy.requests == []


# --- the egress gate ---------------------------------------------------------------------------


def cloud(spy_answer: dict[str, Any] | None = None) -> Spy:
    """The fake under a cloud provider's name, so the route is gated as a real one would be."""
    return Spy(name="anthropic", answer=spy_answer)


@pytest.mark.parametrize("by_label", [False, True])
def test_a_local_only_document_is_withheld_from_a_cloud_provider(
    engine: Engine, by_label: bool
) -> None:
    syllabus, _ = add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    private, private_chunks = add_document(engine, PRIVATE_TITLE, PRIVATE, local_only=not by_label)
    if by_label:
        add_local_only_label(engine, private)
    spy = cloud()
    result = run(engine, spy, config=settings(CLOUD_ROUTES))

    assert result.outcome is Outcome.ANSWERED
    assert result.provider == "anthropic"
    assert [source.document_id for source in result.sources] == [syllabus]
    assert [source.label for source in result.sources] == ["S1"]  # labelled again, no gap
    assert (result.sources_found, result.sources_withheld) == (2, 1)
    reason = Withheld.LABEL_LOCAL_ONLY if by_label else Withheld.DOCUMENT_LOCAL_ONLY
    assert result.withheld == tuple((chunk_id, reason) for chunk_id in private_chunks)
    (request,) = spy.requests
    assert [source.label for source in request.sources] == ["S1"]
    text = sent_text(request)
    assert PRIVATE_TITLE not in text
    assert "Vellichor" not in text
    assert "penicillin" not in text
    (row,) = ledger(engine)
    assert (row.provider, row.doc_count) == ("anthropic", 1)


def test_when_the_gate_withholds_every_source_no_call_is_made(engine: Engine) -> None:
    _, private_chunks = add_document(engine, PRIVATE_TITLE, PRIVATE, local_only=True)
    spy = cloud()
    result = run(engine, spy, config=settings(CLOUD_ROUTES))
    assert result.outcome is Outcome.ALL_WITHHELD
    assert result.insufficient_evidence is True
    assert (result.sources_found, result.sources_withheld) == (1, 1)
    assert [chunk_id for chunk_id, _ in result.withheld] == private_chunks
    assert spy.requests == []
    assert ledger(engine) == []


def test_a_source_is_sent_only_if_the_gate_allows_every_chunk_of_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = "The second page lists the room for the makeup exam."
    _, chunk_ids = add_document(engine, SYLLABUS_TITLE, SYLLABUS, second)
    real = egress.check_egress

    def withhold_the_last(engine: Engine, ids_in: list[int], mode: EgressMode) -> EgressDecision:
        decision = real(engine, ids_in, mode)
        return EgressDecision(
            mode=mode,
            allowed=decision.allowed[:-1],
            withheld=((decision.allowed[-1], Withheld.DATABASE_ERROR),),
            refused=False,
            duplicates=0,
        )

    monkeypatch.setattr(ask_module, "check_egress", withhold_the_last)
    spy = cloud()
    result = run(engine, spy, config=settings(CLOUD_ROUTES, context_neighbor_chunks=1))
    assert result.outcome is Outcome.ALL_WITHHELD
    assert result.withheld == ((chunk_ids[1], Withheld.DATABASE_ERROR),)
    assert spy.requests == []


def test_the_offline_provider_is_not_gated(engine: Engine) -> None:
    private, _ = add_document(engine, PRIVATE_TITLE, PRIVATE, local_only=True)
    spy = Spy()
    result = run(engine, spy)
    assert [source.document_id for source in result.sources] == [private]
    assert result.withheld == ()


def test_every_provider_but_the_offline_one_is_gated() -> None:
    assert {"fake"} == ask_module.LOCAL_PROVIDERS
    assert ask_module.LOCAL_PROVIDERS < PROVIDER_NAMES


# --- failures ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_a_provider_error_is_fixed_text_and_still_recorded(engine: Engine, kind: ErrorKind) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    spy = Spy(inner=FakeProvider(failure=kind))
    with pytest.raises(AskFailedError) as raised:
        run(engine, spy)
    error = raised.value
    assert (error.failure, error.kind) == (AskFailure.PROVIDER_ERROR, kind)
    assert error.retryable is LLMError(kind).retryable
    assert str(error) == f"the provider could not answer: {LLMError(kind)}"
    (row,) = ledger(engine)
    assert (row.status, row.error_kind, row.doc_count) == ("error", kind.value, 1)
    assert (row.input_tokens, row.output_tokens, row.est_cost_nanousd) == (None, None, None)


def test_an_adapter_that_breaks_its_contract_is_recorded_as_a_bad_response(
    engine: Engine,
) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    spy = Spy(raises=RuntimeError(f"provider said: {PRIVATE}"))
    with pytest.raises(AskFailedError) as raised:
        run(engine, spy)
    assert (raised.value.failure, raised.value.kind) == (
        AskFailure.PROVIDER_ERROR,
        ErrorKind.BAD_RESPONSE,
    )
    assert "Vellichor" not in str(raised.value)
    assert raised.value.__cause__ is None
    (row,) = ledger(engine)
    assert (row.status, row.error_kind) == ("error", "bad_response")


def test_an_answer_cut_off_at_the_output_limit_fails_after_it_is_recorded(
    engine: Engine,
) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    with pytest.raises(AskFailedError) as raised:
        run(engine, Spy(), max_output_tokens=5)
    assert raised.value.failure is AskFailure.CUT_OFF
    (row,) = ledger(engine)
    assert (row.status, row.output_tokens) == ("ok", 5)


@pytest.mark.parametrize(
    ("answer", "stop", "failure"),
    [
        ({"answer": []}, StopReason.REFUSED, AskFailure.REFUSED),
        (
            {"answer": "S1", "insufficient_evidence": False},
            StopReason.COMPLETE,
            AskFailure.BAD_ANSWER,
        ),
    ],
)
def test_a_refusal_or_a_misshapen_answer_fails_after_it_is_recorded(
    engine: Engine, answer: dict[str, Any], stop: StopReason, failure: AskFailure
) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    with pytest.raises(AskFailedError) as raised:
        run(engine, Spy(answer=answer, stop=stop))
    assert raised.value.failure is failure
    assert [row.status for row in ledger(engine)] == ["ok"]


def test_a_ledger_that_cannot_be_written_fails_the_question(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise LedgerError

    monkeypatch.setattr(ask_module, "record_call", broken)
    with pytest.raises(AskFailedError) as raised:
        run(engine, Spy())
    assert raised.value.failure is AskFailure.LEDGER_FAILED


def test_a_database_error_does_not_repeat_the_question(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError(f"no such table near {QUESTION}")

    monkeypatch.setattr(ask_module, "rank_chunks", broken)
    spy = Spy()
    with pytest.raises(AskFailedError) as raised:
        run(engine, spy)
    assert raised.value.failure is AskFailure.RETRIEVAL_FAILED
    assert "Petrichor" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert spy.requests == []


def test_damaged_chunk_rows_fail_retrieval_with_fixed_text(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    with engine.begin() as connection:
        connection.execute(update(chunks).values(section_path='["Canary-Sonder", 7]'))
    with pytest.raises(AskFailedError) as raised:
        run(engine, Spy())
    assert raised.value.failure is AskFailure.RETRIEVAL_FAILED
    assert "Sonder" not in str(raised.value)


@pytest.mark.parametrize(
    "question",
    [
        "",
        "   \n\t",
        "x" * (MAX_QUESTION_CHARS + 1),
        " ".join(["Canary-Petrichor"] * (MAX_QUESTION_TERMS + 1)),
        "final\x00exam Canary-Petrichor",
        "final \ud800 exam Canary-Petrichor",
    ],
)
def test_an_invalid_question_is_refused_before_anything_is_read(
    engine: Engine, question: str
) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    spy = Spy()
    with pytest.raises(AskFailedError) as raised:
        run(engine, spy, question=question)
    assert raised.value.failure is AskFailure.INVALID_QUESTION
    assert "Petrichor" not in str(raised.value)
    assert spy.requests == []


def test_the_longest_question_allowed_is_asked(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    words = ["exam"] * MAX_QUESTION_TERMS
    question = " ".join(words)
    assert len(question) <= MAX_QUESTION_CHARS
    assert run(engine, Spy(), question=question).outcome is Outcome.ANSWERED


def test_a_task_with_no_route_and_a_provider_with_no_adapter_fail_before_any_call(
    engine: Engine,
) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    no_route = Settings.model_validate({"ai": {"routes": {"answer": None}}})
    with pytest.raises(AskFailedError) as raised:
        ask(engine, no_route, QUESTION)
    assert raised.value.failure is AskFailure.NO_ROUTE
    # The default route is Anthropic, which has no adapter in this version.
    with pytest.raises(AskFailedError) as raised:
        ask(engine, Settings(), QUESTION)
    assert raised.value.failure is AskFailure.PROVIDER_UNAVAILABLE
    assert ledger(engine) == []


def test_the_registry_serves_the_offline_provider(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    result = ask(engine, settings(), QUESTION)
    assert (result.outcome, result.provider) == (Outcome.ANSWERED, "fake")


@pytest.mark.parametrize(
    "changes",
    [
        {"question": b"bytes"},
        {"deep": 1},
        {"override": {"provider": "fake", "model": "fake-1"}},
    ],
)
def test_arguments_of_the_wrong_type_are_programming_errors(
    engine: Engine, changes: dict[str, Any]
) -> None:
    options: dict[str, Any] = {"question": QUESTION} | changes
    question = options.pop("question")
    with pytest.raises(TypeError):
        ask(engine, settings(), question, **options)


def test_a_provider_reporting_another_name_is_refused(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    with pytest.raises(RuntimeError):
        run(engine, Spy(name="openai"))


# --- retrieval ---------------------------------------------------------------------------------


def test_a_question_matches_any_of_its_terms() -> None:
    assert build_match("final exam", any_term=True) == '"final" OR "exam"'
    assert build_match("CSE485 exam", any_term=True) == '("CSE485" OR "CSE 485") OR "exam"'
    assert build_match("CSE 485 exam", any_term=True) == '(("CSE" AND "485") OR "CSE485") OR "exam"'


def test_the_per_document_cap_applies_to_the_keyword_list(engine: Engine) -> None:
    pages = [f"Page {n} says the exam covers chapter {n}." for n in range(1, 7)]
    add_document(engine, SYLLABUS_TITLE, *pages)
    result = run(
        engine,
        Spy(),
        question="exam",
        config=settings(max_chunks_per_document=2, context_neighbor_chunks=0),
    )
    assert sum(len(source.chunk_ids) for source in result.sources) == 2


def test_a_document_without_a_name_is_sent_as_untitled(engine: Engine) -> None:
    add_document(engine, SYLLABUS_TITLE, SYLLABUS)
    with engine.begin() as connection:
        connection.execute(import_sources.delete())
    spy = Spy()
    result = run(engine, spy)
    assert [source.title for source in result.sources] == [ask_module.UNTITLED]
    assert spy.requests[0].sources[0].title == ask_module.UNTITLED


# --- what is never written ---------------------------------------------------------------------


def formatted(caplog: pytest.LogCaptureFixture) -> str:
    formatter = SafeFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


def test_logs_errors_and_reprs_hold_no_question_passage_title_or_answer(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    add_document(engine, SYLLABUS_TITLE, SYLLABUS, section=SECTION)
    add_document(engine, PRIVATE_TITLE, PRIVATE, local_only=True)
    add_document(engine, NOTES_TITLE, NOTES)
    answer = {
        "answer": [
            {
                "text": "Canary-Petrichor: the exam is on December 10 (Quillfeather).",
                "sources": ["S1", "S4"],
                "quote": "December 10",
            }
        ],
        "insufficient_evidence": False,
        "missing": None,
    }
    results = [
        run(engine, Spy()),
        run(engine, cloud(answer), config=settings(CLOUD_ROUTES)),
        run(engine, cloud(), question="Canary-Vellichor penicillin", config=settings(CLOUD_ROUTES)),
    ]
    errors = []
    for spy in (
        Spy(inner=FakeProvider(failure=ErrorKind.TIMEOUT)),
        Spy(raises=ValueError(PRIVATE)),
    ):
        with pytest.raises(AskFailedError) as raised:
            run(engine, spy)
        errors.append(str(raised.value))
    with pytest.raises(AskFailedError) as raised:
        run(engine, Spy(), question="Canary-Petrichor " * (MAX_QUESTION_TERMS + 1))
    errors.append(str(raised.value))

    written = formatted(caplog)
    assert " ask.answered " in written
    assert " ask.no_call " in written
    assert " ask.failed " in written
    shown = "\n".join([written, *errors, *(repr(result) for result in results)])
    for canary in CANARIES:
        assert canary not in shown, canary
    assert "December" not in shown
    assert "Room 201" not in shown
