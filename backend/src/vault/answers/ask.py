"""Ask the Vault as one call: question in, validated cited answer out (plan sections 8 to 11,
ADRs 0009, 0010 and 0011). No HTTP route and no screen yet; :func:`ask` is what they will call.

The steps, each one a module that already exists:

1. **Question.** Text with a character other than white space, at most
   :data:`MAX_QUESTION_CHARS` characters and :data:`MAX_QUESTION_TERMS` terms, which SQLite can
   store. Anything else is :attr:`AskFailure.INVALID_QUESTION`.
2. **Route.** :func:`vault.ai.routing.route` for :attr:`Task.ANSWER`, or :attr:`Task.ANSWER_DEEP`
   with ``deep``, or the Ask screen's per-question ``override``; then the provider is created
   (:func:`vault.ai.providers.registry.create_provider`), so a route that cannot be used fails
   before anything is read.
3. **Retrieval** (section 8, steps 1 to 4 and 6), in one read snapshot. Keyword search is what
   exists today: :func:`vault.retrieval.keyword.rank_chunks` lists the chunks of the documents
   in use (current versions, not in the trash) that match *any* term of the question
   (``build_match(any_term=True)``: a question is a sentence, and requiring every word would find
   almost nothing), best BM25 first, up to ``[retrieval] keyword_candidates``. The list goes
   through Reciprocal Rank Fusion with an empty vector list (:func:`vault.retrieval.fusion.fuse`),
   which keeps its order, so the vector list joins here when an embedding model is chosen
   (ADR 0013 is Proposed) without another change, and then the per-document cap
   (``max_chunks_per_document``). The chunks of the hits and their neighbours (up to
   ``context_neighbor_chunks`` on each side, same version) are loaded with the documents' names.
4. **Packing** (steps 6 and 7): :func:`vault.retrieval.packing.pack` into
   ``context_budget_tokens``.
5. **Egress gate** (section 10, ADR 0010), for every provider except those that send nothing
   anywhere (:data:`LOCAL_PROVIDERS`: the offline fake), so a provider added later is gated
   unless it is listed here. Every chunk of every packed source goes to
   :func:`vault.ai.egress.check_egress` in :attr:`EgressMode.DROP` mode, immediately before the
   request is built, and a source is sent only if *all* of its chunks are allowed. The plan
   allows refusing the call or dropping the restricted chunks "and telling you"; dropping is the
   mode here because the answer can still use what may be sent, and the result says what was
   withheld (``sources_withheld`` and ``withheld``, ids and reasons only), which the Ask screen
   shows. The sources left are labelled again, ``S1`` to ``Sn`` in their order, so the labels the
   model sees have no gaps. Retrieval does not leave Local Only documents out before packing
   yet (the plan's "later" in step 1), so a withheld source's budget is not refilled.
6. **No passages, no call.** When retrieval finds nothing, or the gate withholds every source,
   no model is called and no usage row is written: the result is
   :attr:`Outcome.NO_PASSAGES` or :attr:`Outcome.ALL_WITHHELD` with ``insufficient_evidence``.
7. **Request and call.** :func:`vault.ai.answer_prompt.build_answer_request` with the routed
   model and :data:`ANSWER_MAX_OUTPUT_TOKENS`, then ``generate`` (not streaming; the streaming
   Ask screen will use ``stream`` with the same steps around it).
8. **Usage.** :func:`vault.ai.usage.record_call` once for the call, whatever happened: the
   response, the :class:`LLMError`, or, for an adapter that broke its contract by raising
   anything else, :attr:`ErrorKind.BAD_RESPONSE`. ``doc_count`` is the number of documents
   whose passages were sent. A ledger that cannot be written fails the question
   (:attr:`AskFailure.LEDGER_FAILED`), because budgets will be counted from it.
9. **Answer.** A provider error is :attr:`AskFailure.PROVIDER_ERROR` with its kind. A response cut
   at ``max_output_tokens``, refused, or not the cited-answer shape is ``CUT_OFF``, ``REFUSED`` or
   ``BAD_ANSWER``. Otherwise :func:`vault.answers.citations.validate_answer` checks the labels
   and quotes (an invented label is removed and its segment marked unsupported), and the result
   holds the validated answer, every source sent with whether it was cited, the prompt id, the
   provider and model, and ``insufficient_evidence`` (the model's flag, or no segment left).

**Errors.** Every failure the owner can cause or meet is an :class:`AskFailedError` with a fixed
text per :class:`AskFailure`; a database error is raised without its cause, because SQLAlchemy's
message repeats the bound parameters, which hold the question. Arguments of the wrong type are
programming errors (``TypeError``).

**Logs.** One ``ask.answered``, ``ask.no_call`` or ``ask.failed`` line with the task, the outcome
or failure, and counts. The question, the passages, the titles, section titles and the answer
are never logged, and no error repeats them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Final

from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import SQLAlchemyError

from vault.ai.answer import read_answer
from vault.ai.answer_prompt import build_answer_request
from vault.ai.egress import EgressMode, Withheld, check_egress
from vault.ai.providers.base import (
    ErrorKind,
    LLMError,
    LLMProvider,
    LLMResponse,
    SourceBlock,
    StopReason,
    Task,
)
from vault.ai.providers.registry import ProviderUnavailableError, create_provider
from vault.ai.routing import InvalidChoiceError, ModelChoice, RouteError, route
from vault.ai.usage import LedgerError, record_call
from vault.answers.citations import (
    Citation,
    SentSource,
    SourceMap,
    ValidatedAnswer,
    validate_answer,
)
from vault.core.config import RetrievalSettings, Settings
from vault.core.logging import log_event, metric
from vault.db.engine import DatabaseError, read_snapshot
from vault.db.schema import chunks
from vault.retrieval.fusion import FusionError, Hit, cap_per_document, fuse
from vault.retrieval.keyword import QueryError, build_match, rank_chunks
from vault.retrieval.packing import (
    ContextChunk,
    PackedContext,
    PackedSource,
    PackingError,
    SourcePart,
    pack,
)

logger = logging.getLogger(__name__)

# Far above a typed question, and the bound on the work one question asks of the index.
MAX_QUESTION_CHARS: Final = 2000
MAX_QUESTION_TERMS: Final = 64
# The answer's output budget. The plan names none; a cited answer of the plan's size (about 600
# tokens of text, plus a quote per segment and the JSON around them) fits several times over.
ANSWER_MAX_OUTPUT_TOKENS: Final = 4096
# The providers that send nothing off the machine, so the egress gate does not apply. Every
# other provider name is gated.
LOCAL_PROVIDERS: Final = frozenset({"fake"})
# A document whose name is blank (no import source) is shown to the model under this name.
UNTITLED: Final = "Untitled document"

type ProviderFactory = Callable[[str], LLMProvider]


class Outcome(Enum):
    ANSWERED = "answered"  # a model was called and its answer validated
    NO_PASSAGES = "no_passages"  # retrieval found nothing to send: no call
    ALL_WITHHELD = "all_withheld"  # the egress gate withheld every source: no call


class AskFailure(Enum):
    INVALID_QUESTION = "invalid_question"
    NO_ROUTE = "no_route"
    INVALID_CHOICE = "invalid_choice"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RETRIEVAL_FAILED = "retrieval_failed"
    PROVIDER_ERROR = "provider_error"
    CUT_OFF = "cut_off"
    REFUSED = "refused"
    BAD_ANSWER = "bad_answer"
    LEDGER_FAILED = "ledger_failed"


_FAILURE_TEXT: Final = MappingProxyType(
    {
        AskFailure.INVALID_QUESTION: (
            f"the question must be text of at most {MAX_QUESTION_CHARS} characters"
            f" and {MAX_QUESTION_TERMS} terms"
        ),
        AskFailure.NO_ROUTE: "no model is configured for this task",
        AskFailure.INVALID_CHOICE: "the provider or model chosen is not a valid choice",
        AskFailure.PROVIDER_UNAVAILABLE: (
            "this provider is not available in this version of the app"
        ),
        AskFailure.RETRIEVAL_FAILED: "the vault could not be searched for this question",
        AskFailure.PROVIDER_ERROR: "the provider could not answer",
        AskFailure.CUT_OFF: "the answer was longer than the output limit and was cut off",
        AskFailure.REFUSED: "the model declined to answer",
        AskFailure.BAD_ANSWER: "the model's answer did not have the cited-answer shape",
        AskFailure.LEDGER_FAILED: "the usage ledger could not be written",
    }
)


class AskFailedError(Exception):
    """A question that could not be answered. The message is fixed text for the failure (and,
    for a provider error, its kind); it repeats nothing from the question or the vault."""

    def __init__(self, failure: AskFailure, kind: ErrorKind | None = None) -> None:
        message = _FAILURE_TEXT[failure]
        if kind is not None:
            message = f"{message}: {LLMError(kind)}"
        super().__init__(message)
        self.failure = failure
        self.kind = kind

    @property
    def retryable(self) -> bool:
        """Whether the same question may succeed later (a rate limit, an outage, a timeout)."""
        return self.kind is not None and LLMError(self.kind).retryable


@dataclass(frozen=True)
class AskSource:
    """A passage as it was sent to the model under ``label``: what the AI saw (section 11, step
    5). ``parts`` map spans of ``text`` to chunks and pages (``vault.retrieval.packing``)."""

    label: str
    document_id: str
    version_id: str
    title: str = field(repr=False)
    page_start: int
    page_end: int
    section_path: tuple[str, ...] = field(repr=False)
    text: str = field(repr=False)
    parts: tuple[SourcePart, ...]
    # Whether a segment kept after validation cites this label.
    cited: bool

    @property
    def chunk_ids(self) -> tuple[int, ...]:
        return tuple(part.chunk_id for part in self.parts)


@dataclass(frozen=True)
class AskResult:
    """What :func:`ask` returns. ``validated`` is ``None`` exactly when no model was called."""

    outcome: Outcome
    task: Task
    provider: str
    model: str
    # The prompt the request was built from (``answer.v1``), to store with the result.
    prompt_id: str | None
    # Every source sent, S1 first. Empty when no model was called.
    sources: tuple[AskSource, ...]
    validated: ValidatedAnswer | None
    insufficient_evidence: bool
    # Sources packed before the gate, and those it withheld (not sent, not in ``sources``).
    sources_found: int
    sources_withheld: int
    # The chunks the gate withheld, by id, with the reason.
    withheld: tuple[tuple[int, Withheld], ...]
    # The ``ai_requests`` row of the call.
    ledger_id: int | None

    def source(self, label: str) -> AskSource | None:
        return next((source for source in self.sources if source.label == label), None)

    def chunks_for(self, citation: Citation) -> tuple[int, ...]:
        """The chunks a citation stands for: those its verified quote touches, or every chunk of
        the cited source when the quote was not found."""
        source = self.source(citation.label)
        if source is None:
            return ()
        if citation.quote_span is None:
            return source.chunk_ids
        start, end = citation.quote_span
        return tuple(
            part.chunk_id
            for part in source.parts
            if part.source_start < end and start < part.source_end
        )


class _StoredValueError(ValueError):
    """A stored chunk value retrieval cannot use (a damaged database). Fixed text."""


def _section_path(stored: object) -> tuple[str, ...]:
    try:
        titles = json.loads(stored) if isinstance(stored, str) else None
    except ValueError:
        titles = None
    if not isinstance(titles, list) or not all(isinstance(title, str) for title in titles):
        msg = "a stored section path is not a list of titles"
        raise _StoredValueError(msg)
    return tuple(titles)


def _load_chunks(
    connection: Connection, hits: Sequence[Hit], neighbours: int
) -> dict[int, ContextChunk]:
    """The chunks of ``hits`` and up to ``neighbours`` chunks on each side of each, same version."""
    wanted = [hit.chunk_id for hit in hits]
    positions = connection.execute(
        select(chunks.c.id, chunks.c.version_id, chunks.c.chunk_index).where(
            chunks.c.id.in_(wanted)
        )
    ).all()
    loaded: dict[int, ContextChunk] = {}
    for _chunk_id, version_id, index in positions:
        rows = connection.execute(
            select(
                chunks.c.id,
                chunks.c.document_id,
                chunks.c.version_id,
                chunks.c.chunk_index,
                chunks.c.page_start,
                chunks.c.page_end,
                chunks.c.char_start,
                chunks.c.char_end,
                chunks.c.section_path,
                chunks.c.text,
            ).where(
                chunks.c.version_id == version_id,
                chunks.c.chunk_index.between(index - neighbours, index + neighbours),
            )
        ).all()
        for row in rows:
            if row.id in loaded:
                continue
            loaded[row.id] = ContextChunk(
                chunk_id=row.id,
                document_id=row.document_id,
                version_id=row.version_id,
                index=row.chunk_index,
                page_start=row.page_start,
                page_end=row.page_end,
                char_start=row.char_start,
                char_end=row.char_end,
                section_path=_section_path(row.section_path),
                text=row.text,
            )
    return loaded


def _retrieve(engine: Engine, settings: RetrievalSettings, match: str) -> PackedContext:
    with read_snapshot(engine) as connection:
        ranked = rank_chunks(connection, match, settings.keyword_candidates)
        keyword = [Hit(chunk.chunk_id, chunk.document_id) for chunk in ranked]
        capped = cap_per_document(fuse(keyword, (), settings), settings)
        titles: dict[str, str] = {}
        for chunk in ranked:
            name = chunk.name if isinstance(chunk.name, str) and chunk.name.strip() else UNTITLED
            titles.setdefault(chunk.document_id, name)
        loaded = _load_chunks(
            connection,
            [Hit(hit.chunk_id, hit.document_id) for hit in capped],
            settings.context_neighbor_chunks,
        )
    return pack(capped, loaded, titles, settings)


def _gate(
    engine: Engine, sources: Sequence[PackedSource]
) -> tuple[list[PackedSource], tuple[tuple[int, Withheld], ...]]:
    """The sources all of whose chunks the gate allows, and the chunks it withheld."""
    chunk_ids = [part.chunk_id for source in sources for part in source.parts]
    decision = check_egress(engine, chunk_ids, EgressMode.DROP)
    allowed = set(decision.allowed)
    kept = [s for s in sources if all(part.chunk_id in allowed for part in s.parts)]
    return kept, decision.withheld


def _fail(failure: AskFailure, task: Task, kind: ErrorKind | None = None) -> AskFailedError:
    log_event(logger, "ask.failed", level=logging.WARNING, task=task, failure=failure, kind=kind)
    return AskFailedError(failure, kind)


def _check_arguments(
    engine: object, settings: object, question: object, deep: object, override: object
) -> None:
    if not isinstance(engine, Engine):
        msg = "engine must be an Engine"
        raise TypeError(msg)
    if not isinstance(settings, Settings):
        msg = "settings must be Settings"
        raise TypeError(msg)
    if not isinstance(question, str):
        msg = "the question must be a str"
        raise TypeError(msg)
    if type(deep) is not bool:
        msg = "deep must be a bool"
        raise TypeError(msg)
    if override is not None and type(override) is not ModelChoice:
        msg = "an override must be a ModelChoice"
        raise TypeError(msg)


def ask(
    engine: Engine,
    settings: Settings,
    question: str,
    *,
    deep: bool = False,
    override: ModelChoice | None = None,
    providers: ProviderFactory = create_provider,
    max_output_tokens: int = ANSWER_MAX_OUTPUT_TOKENS,
) -> AskResult:
    """Answer ``question`` from the vault with validated citations (see the module docstring).

    ``providers`` turns a provider name into a provider (the registry's ``create_provider``;
    tests pass their own). Raises :class:`AskFailedError` with fixed text."""
    _check_arguments(engine, settings, question, deep, override)
    task = Task.ANSWER_DEEP if deep else Task.ANSWER

    if not question.strip():
        raise _fail(AskFailure.INVALID_QUESTION, task)
    try:
        # None: the question has no letter or digit, so nothing can match it.
        match = build_match(
            question, any_term=True, max_chars=MAX_QUESTION_CHARS, max_terms=MAX_QUESTION_TERMS
        )
    except QueryError:
        raise _fail(AskFailure.INVALID_QUESTION, task) from None

    try:
        chosen = route(task, settings, override)
    except RouteError:
        raise _fail(AskFailure.NO_ROUTE, task) from None
    except InvalidChoiceError:
        raise _fail(AskFailure.INVALID_CHOICE, task) from None
    try:
        provider = providers(chosen.provider)
    except ProviderUnavailableError:
        raise _fail(AskFailure.PROVIDER_UNAVAILABLE, task) from None
    if provider.name != chosen.provider:
        msg = "the provider reports another name than the route names"
        raise RuntimeError(msg)

    def no_call(
        outcome: Outcome, found: int, withheld: tuple[tuple[int, Withheld], ...]
    ) -> AskResult:
        log_event(
            logger,
            "ask.no_call",
            task=task,
            outcome=outcome,
            sources_found=metric(found),
            chunks_withheld=metric(len(withheld)),
        )
        return AskResult(
            outcome=outcome,
            task=task,
            provider=chosen.provider,
            model=chosen.model,
            prompt_id=None,
            sources=(),
            validated=None,
            insufficient_evidence=True,
            sources_found=found,
            sources_withheld=found,
            withheld=withheld,
            ledger_id=None,
        )

    if match is None:
        return no_call(Outcome.NO_PASSAGES, 0, ())
    try:
        context = _retrieve(engine, settings.retrieval, match)
    except (
        SQLAlchemyError,
        sqlite3.Error,
        DatabaseError,
        FusionError,
        PackingError,
        _StoredValueError,
    ):
        log_event(logger, "ask.retrieval_failed", level=logging.ERROR, exc_info=True, task=task)
        raise _fail(AskFailure.RETRIEVAL_FAILED, task) from None
    found = len(context.sources)
    if not found:
        return no_call(Outcome.NO_PASSAGES, 0, ())

    withheld: tuple[tuple[int, Withheld], ...] = ()
    packed = list(context.sources)
    if chosen.provider not in LOCAL_PROVIDERS:
        packed, withheld = _gate(engine, packed)
        if not packed:
            return no_call(Outcome.ALL_WITHHELD, found, withheld)

    labels = [f"S{number}" for number in range(1, len(packed) + 1)]
    try:
        blocks = [
            SourceBlock(label, s.title, s.text, s.page_start, s.page_end)
            for label, s in zip(labels, packed, strict=True)
        ]
        sent = SourceMap(
            [
                SentSource(
                    chunk_id=s.parts[0].chunk_id,
                    version_id=s.version_id,
                    document_id=s.document_id,
                    page_start=s.page_start,
                    page_end=s.page_end,
                    text=s.text,
                )
                for s in packed
            ]
        )
    except ValueError:
        # Only a damaged database gives a packed source blank text or a blank title.
        raise _fail(AskFailure.RETRIEVAL_FAILED, task) from None
    built = build_answer_request(
        question, blocks, model=chosen.model, max_output_tokens=max_output_tokens, task=task
    )
    doc_count = len({s.document_id for s in packed})

    started = time.monotonic()
    outcome: LLMResponse | LLMError
    try:
        outcome = provider.generate(built.request)
    except LLMError as error:
        outcome = error
    except Exception:  # an adapter's contract breach is still a call to record
        log_event(logger, "ask.provider_broke_contract", level=logging.ERROR, exc_info=True)
        outcome = LLMError(ErrorKind.BAD_RESPONSE)
    elapsed = int((time.monotonic() - started) * 1000)
    try:
        entry = record_call(
            engine,
            settings.pricing,
            provider=provider.name,
            request=built.request,
            outcome=outcome,
            doc_count=doc_count,
            latency_ms=elapsed if isinstance(outcome, LLMError) else None,
        )
    except LedgerError:
        raise _fail(AskFailure.LEDGER_FAILED, task) from None
    if isinstance(outcome, LLMError):
        raise _fail(AskFailure.PROVIDER_ERROR, task, outcome.kind)
    if outcome.stop_reason is StopReason.MAX_TOKENS:
        raise _fail(AskFailure.CUT_OFF, task)
    if outcome.stop_reason is StopReason.REFUSED:
        raise _fail(AskFailure.REFUSED, task)
    if outcome.structured is None:
        raise _fail(AskFailure.BAD_ANSWER, task)
    try:
        structured = read_answer(outcome.structured)
    except LLMError:
        raise _fail(AskFailure.BAD_ANSWER, task) from None

    validated = validate_answer(structured, sent)
    cited = {c.label for segment in validated.segments for c in segment.citations}
    sources = tuple(
        AskSource(
            label=label,
            document_id=s.document_id,
            version_id=s.version_id,
            title=s.title,
            page_start=s.page_start,
            page_end=s.page_end,
            section_path=s.section_path,
            text=s.text,
            parts=s.parts,
            cited=label in cited,
        )
        for label, s in zip(labels, packed, strict=True)
    )
    statuses: dict[str, int] = {}
    for segment in validated.segments:
        statuses[segment.status.value] = statuses.get(segment.status.value, 0) + 1
    log_event(
        logger,
        "ask.answered",
        task=task,
        request_id=metric(entry.id),
        sources_found=metric(found),
        sources_sent=metric(len(sources)),
        chunks_withheld=metric(len(withheld)),
        documents=metric(doc_count),
        segments=metric(len(validated.segments)),
        supported=metric(statuses.get("supported", 0)),
        unverified=metric(statuses.get("unverified", 0)),
        unsupported=metric(statuses.get("unsupported", 0)),
        uncited=metric(statuses.get("uncited", 0)),
        findings=metric(len(validated.findings)),
        insufficient_evidence=validated.insufficient_evidence,
    )
    return AskResult(
        outcome=Outcome.ANSWERED,
        task=task,
        provider=chosen.provider,
        model=chosen.model,
        prompt_id=built.prompt_id,
        sources=sources,
        validated=validated,
        insufficient_evidence=validated.insufficient_evidence,
        sources_found=found,
        sources_withheld=found - len(sources),
        withheld=withheld,
        ledger_id=entry.id,
    )
