"""The provider contract (plan section 9, Phase 5 testing: "same suite against both adapters").

Every test takes ``subject``, a :class:`Subject` that builds the provider under test, healthy or
failing with a given :class:`ErrorKind`. The fake and the Anthropic adapter are here; the
Anthropic adapter runs on recorded responses (``tests/fixtures/llm_recordings/anthropic/``,
replayed by ``anthropic_replay.Replay`` through ``httpx2.MockTransport``), so the suite needs no
key and no network. The OpenAI adapter joins the same way. A test here asserts only what every
provider must do; what is particular to the fake is in ``test_fake_provider.py``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from vault.ai.answer import ANSWER_SCHEMA, read_answer
from vault.ai.providers.anthropic import AnthropicProvider
from vault.ai.providers.base import (
    Completed,
    ErrorKind,
    LLMError,
    LLMEvent,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    SourceBlock,
    StopReason,
    Task,
    TextDelta,
    parse_structured,
)
from vault.ai.providers.fake import FakeProvider

from .anthropic_replay import Replay, SyntheticKey

# Synthetic text only. The marker lets a test look for the request's text in log output.
MARKER = "zqxjv-private-marker"


@dataclass(frozen=True)
class Subject:
    name: str
    make: Callable[[ErrorKind | None, int], LLMProvider]
    model: str
    # Streams started and not ended, or None if the adapter cannot tell.
    open_streams: Callable[[LLMProvider], int | None]


def _fake_open(provider: LLMProvider) -> int | None:
    assert isinstance(provider, FakeProvider)
    return provider.open_streams


def _anthropic(failure: ErrorKind | None, after: int) -> LLMProvider:
    replay = Replay(failure=failure, after=after)
    return AnthropicProvider(SyntheticKey(), http_client=replay.client(), max_retries=0)


def _anthropic_open(provider: LLMProvider) -> int | None:
    assert isinstance(provider, AnthropicProvider)
    return provider.open_streams


SUBJECTS = {
    "anthropic": Subject(
        name="anthropic",
        make=_anthropic,
        model="claude-sonnet-5-5",
        open_streams=_anthropic_open,
    ),
    "fake": Subject(
        name="fake",
        make=lambda failure, after: FakeProvider(failure=failure, failure_after_deltas=after),
        model="fake-1",
        open_streams=_fake_open,
    ),
}


@pytest.fixture(params=sorted(SUBJECTS))
def subject(request: pytest.FixtureRequest) -> Subject:
    return SUBJECTS[request.param]


def _request(
    subject: Subject,
    *,
    sources: int = 2,
    schema: bool = True,
    max_output_tokens: int = 2000,
) -> LLMRequest:
    blocks = tuple(
        SourceBlock(
            label=f"S{n}",
            title=f"Synthetic syllabus {n}",
            text=f"The project milestone {n} is due on day {n * 7}. {MARKER} follows here.",
            page_start=n,
            page_end=n,
        )
        for n in range(1, sources + 1)
    )
    return LLMRequest(
        task=Task.ANSWER,
        model=subject.model,
        system="Answer only from the sources and cite each claim.",
        messages=(Message(Role.USER, f"When is the milestone due? {MARKER}"),),
        sources=blocks,
        output_schema=ANSWER_SCHEMA if schema else None,
        max_output_tokens=max_output_tokens,
    )


def _check_response(req: LLMRequest, response: LLMResponse) -> None:
    """What every response to ``req`` must satisfy, whichever provider gave it."""
    assert isinstance(response, LLMResponse)
    assert response.usage.input > 0
    assert 0 <= response.usage.cached <= response.usage.input
    assert response.usage.output <= req.max_output_tokens
    assert response.latency_ms >= 0
    if req.output_schema is not None and response.stop_reason is StopReason.COMPLETE:
        assert response.structured is not None
        assert response.structured == parse_structured(response.text)
    else:
        assert response.structured is None


def _drain(events: list[LLMEvent]) -> LLMResponse:
    """Check the stream's order: deltas, then exactly one Completed, last; return its response."""
    assert events, "a stream yields at least the Completed event"
    *deltas, last = events
    assert isinstance(last, Completed)
    assert all(isinstance(event, TextDelta) for event in deltas)
    assert "".join(event.text for event in deltas if isinstance(event, TextDelta)) == (
        last.response.text
    )
    return last.response


def test_provider_has_a_name(subject: Subject) -> None:
    provider = subject.make(None, 0)
    assert isinstance(provider.name, str)
    assert provider.name == subject.name


def test_generate_answers_in_the_cited_shape(subject: Subject) -> None:
    req = _request(subject)
    response = subject.make(None, 0).generate(req)
    _check_response(req, response)
    assert response.stop_reason is StopReason.COMPLETE
    assert response.structured is not None
    answer = read_answer(response.structured)
    labels = {source.label for source in req.sources}
    assert answer.answer
    assert all(set(segment.sources) <= labels for segment in answer.answer)


def test_generate_without_schema_returns_text_only(subject: Subject) -> None:
    req = _request(subject, schema=False)
    response = subject.make(None, 0).generate(req)
    _check_response(req, response)
    assert response.text.strip()


def test_answer_without_sources_says_evidence_is_insufficient(subject: Subject) -> None:
    req = _request(subject, sources=0)
    response = subject.make(None, 0).generate(req)
    _check_response(req, response)
    assert response.structured is not None
    assert read_answer(response.structured).insufficient_evidence is True


def test_stream_yields_deltas_then_one_completed(subject: Subject) -> None:
    req = _request(subject)
    provider = subject.make(None, 0)
    response = _drain(list(provider.stream(req)))
    _check_response(req, response)
    assert subject.open_streams(provider) in {0, None}


def test_stream_without_schema_has_the_same_order(subject: Subject) -> None:
    req = _request(subject, schema=False)
    response = _drain(list(subject.make(None, 0).stream(req)))
    _check_response(req, response)


def test_cut_off_answer_has_no_structured_output(subject: Subject) -> None:
    req = _request(subject, max_output_tokens=5)
    provider = subject.make(None, 0)
    response = provider.generate(req)
    _check_response(req, response)
    assert response.stop_reason is StopReason.MAX_TOKENS
    assert response.structured is None
    streamed = _drain(list(provider.stream(req)))
    _check_response(req, streamed)
    assert streamed.stop_reason is StopReason.MAX_TOKENS


def test_closing_a_stream_cancels_it(subject: Subject) -> None:
    provider = subject.make(None, 0)
    stream = provider.stream(_request(subject))
    first = next(stream)
    assert isinstance(first, TextDelta)
    assert subject.open_streams(provider) in {1, None}
    stream.close()
    assert subject.open_streams(provider) in {0, None}
    with pytest.raises(StopIteration):
        next(stream)


def test_leaving_closing_block_cancels_the_stream(subject: Subject) -> None:
    provider = subject.make(None, 0)
    with contextlib.closing(provider.stream(_request(subject))) as stream:
        assert isinstance(next(stream), TextDelta)
    assert subject.open_streams(provider) in {0, None}


def test_count_tokens_is_a_positive_whole_number(subject: Subject) -> None:
    req = _request(subject)
    count = subject.make(None, 0).count_tokens(req)
    assert isinstance(count, int)
    assert not isinstance(count, bool)
    assert count > 0


def test_more_input_counts_more_tokens(subject: Subject) -> None:
    provider = subject.make(None, 0)
    assert provider.count_tokens(_request(subject, sources=3)) > provider.count_tokens(
        _request(subject, sources=1)
    )


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_every_error_kind_reaches_the_caller_as_llm_error(
    subject: Subject, kind: ErrorKind
) -> None:
    provider = subject.make(kind, 0)
    req = _request(subject)
    with pytest.raises(LLMError) as generated:
        provider.generate(req)
    assert generated.value.kind is kind
    with pytest.raises(LLMError) as streamed:
        list(provider.stream(req))
    assert streamed.value.kind is kind
    assert subject.open_streams(provider) in {0, None}
    with pytest.raises(LLMError) as counted:
        provider.count_tokens(req)
    assert counted.value.kind is kind


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_error_messages_never_repeat_the_request(subject: Subject, kind: ErrorKind) -> None:
    with pytest.raises(LLMError) as raised:
        subject.make(kind, 0).generate(_request(subject))
    assert MARKER not in str(raised.value)
    assert MARKER not in repr(raised.value)
    assert raised.value.__cause__ is None


def test_a_failure_mid_stream_comes_after_the_deltas_already_sent(subject: Subject) -> None:
    provider = subject.make(ErrorKind.UNAVAILABLE, 3)
    received: list[LLMEvent] = []
    with pytest.raises(LLMError) as raised:
        received.extend(provider.stream(_request(subject)))
    assert raised.value.kind is ErrorKind.UNAVAILABLE
    assert raised.value.retryable
    assert len(received) == 3
    assert all(isinstance(event, TextDelta) for event in received)
    assert subject.open_streams(provider) in {0, None}


def test_nothing_from_the_request_or_answer_is_logged(
    subject: Subject, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    provider = subject.make(None, 0)
    req = _request(subject)
    response = provider.generate(req)
    list(provider.stream(req))
    provider.count_tokens(req)
    assert response.text
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert MARKER not in repr(record.__dict__)
