"""What the fake provider does beyond the contract: exact, deterministic answers and counts."""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from vault.ai.answer import ANSWER_SCHEMA, read_answer
from vault.ai.providers.base import (
    Completed,
    ErrorKind,
    LLMError,
    LLMEvent,
    LLMProvider,
    LLMRequest,
    Message,
    Role,
    SourceBlock,
    StopReason,
    Task,
    TextDelta,
    thaw,
)
from vault.ai.providers.fake import EXCERPT_CHARS, MAX_SEGMENTS, FakeProvider, _excerpt
from vault.ingest.chunking.structural_v1 import count_tokens

SOURCES = (
    "The prototype is due on 15 October. Late work loses five points a day.",
    "  Office hours move to Room 204!\nBring the signed form.",
    "Résumé review ends on Friday? Ask the tutor.",
    "A fourth passage the answer does not use.",
)


def _request(n: int = 2, **changes: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "task": Task.ANSWER,
        "model": "fake-1",
        "system": "Answer from the sources.",
        "messages": (Message(Role.USER, "What is due?"),),
        "max_output_tokens": 4000,
        "sources": tuple(SourceBlock(f"S{i + 1}", f"Doc {i + 1}", SOURCES[i]) for i in range(n)),
        "output_schema": ANSWER_SCHEMA,
    }
    fields.update(changes)
    return LLMRequest(**fields)


def test_the_fake_is_an_llm_provider() -> None:
    provider: LLMProvider = FakeProvider()
    assert provider.name == "fake"


def test_the_answer_cites_each_source_with_its_opening_sentence() -> None:
    response = FakeProvider().generate(_request(3))
    assert response.structured is not None
    answer = read_answer(response.structured)
    assert answer.insufficient_evidence is False
    assert answer.missing is None
    assert [(s.text, s.sources, s.quote) for s in answer.answer] == [
        ("The prototype is due on 15 October.", ["S1"], "The prototype is due on 15 October."),
        ("Office hours move to Room 204!", ["S2"], "Office hours move to Room 204!"),
        ("Résumé review ends on Friday?", ["S3"], "Résumé review ends on Friday?"),
    ]


def test_every_quote_is_in_its_source() -> None:
    req = _request(4)
    assert req.sources
    answer = read_answer(FakeProvider().generate(req).structured or {})
    assert len(answer.answer) == MAX_SEGMENTS
    texts = {source.label: source.text for source in req.sources}
    for segment in answer.answer:
        assert segment.quote is not None
        assert segment.quote in texts[segment.sources[0]]


def test_no_sources_means_insufficient_evidence() -> None:
    answer = read_answer(FakeProvider().generate(_request(0)).structured or {})
    assert answer.answer == []
    assert answer.insufficient_evidence is True
    assert answer.missing


@pytest.mark.parametrize(
    ("text", "excerpt"),
    [
        ("One. Two.", "One."),
        ("No end mark here", "No end mark here"),
        ("Version 2.5 is out. Next.", "Version 2.5 is out."),
        ("word " * 60, ("word " * 40).rstrip()),
        ("x" * 300, "x" * EXCERPT_CHARS),
        ("\n  Indented. Rest.", "Indented."),
    ],
)
def test_excerpt_is_the_opening_sentence_or_a_cut_at_a_word(text: str, excerpt: str) -> None:
    assert _excerpt(text) == excerpt
    assert excerpt in text
    assert len(excerpt) <= EXCERPT_CHARS


def test_the_same_request_gives_the_same_response() -> None:
    first = FakeProvider().generate(_request())
    second = FakeProvider().generate(_request())
    assert first == second
    assert first.provider_request_id == second.provider_request_id
    assert first.latency_ms == 0


def test_a_different_request_gets_a_different_request_id() -> None:
    ids = {
        FakeProvider().generate(req).provider_request_id
        for req in [
            _request(),
            _request(model="fake-2"),
            _request(messages=(Message(Role.USER, "What else is due?"),)),
            _request(max_output_tokens=3999),
        ]
    }
    assert len(ids) == 4
    assert all(i is not None and i.startswith("fake_") for i in ids)


def test_usage_is_the_chunker_count_of_input_and_output() -> None:
    req = _request(2)
    response = FakeProvider().generate(req)
    expected_input = (
        count_tokens(req.system)
        + count_tokens("What is due?")
        + sum(count_tokens(s.title) + count_tokens(s.text) for s in req.sources)
        + count_tokens(json.dumps(thaw(ANSWER_SCHEMA), sort_keys=True, separators=(",", ":")))
    )
    assert FakeProvider().count_tokens(req) == expected_input
    assert response.usage.input == expected_input
    assert response.usage.output == count_tokens(response.text)
    assert response.usage.cached == 0


@pytest.mark.parametrize("limit", [1, 2, 7, 30])
def test_a_long_answer_is_cut_at_exactly_max_output_tokens(limit: int) -> None:
    full = FakeProvider().generate(_request())
    response = FakeProvider().generate(_request(max_output_tokens=limit))
    assert response.stop_reason is StopReason.MAX_TOKENS
    assert response.structured is None
    assert response.usage.output == limit
    assert full.text.startswith(response.text)


def test_an_answer_of_exactly_max_output_tokens_is_complete() -> None:
    full = FakeProvider().generate(_request())
    exact = FakeProvider().generate(_request(max_output_tokens=full.usage.output))
    assert exact.stop_reason is StopReason.COMPLETE
    assert exact.text == full.text


def test_plain_text_names_the_task_and_repeats_nothing() -> None:
    req = _request(output_schema=None, task=Task.CLASSIFY)
    response = FakeProvider().generate(req)
    assert response.structured is None
    assert "classify" in response.text
    for source in req.sources:
        assert source.text not in response.text


def test_another_schema_is_an_invalid_request() -> None:
    req = _request(output_schema={"type": "object", "properties": {}})
    with pytest.raises(LLMError) as raised:
        FakeProvider().generate(req)
    assert raised.value.kind is ErrorKind.INVALID_REQUEST
    assert not raised.value.retryable


def test_the_stream_has_one_delta_per_token_then_the_generate_response() -> None:
    req = _request()
    provider = FakeProvider()
    events = list(provider.stream(req))
    deltas = [e for e in events if isinstance(e, TextDelta)]
    last = events[-1]
    assert isinstance(last, Completed)
    assert last.response == provider.generate(req)
    assert len(deltas) == last.response.usage.output
    assert provider.open_streams == 0


def test_failure_after_more_deltas_than_the_answer_has_comes_before_completed() -> None:
    provider = FakeProvider(failure=ErrorKind.TIMEOUT, failure_after_deltas=10_000)
    events: list[LLMEvent] = []
    with pytest.raises(LLMError) as raised:
        events.extend(provider.stream(_request()))
    assert raised.value.kind is ErrorKind.TIMEOUT
    assert events
    assert not any(isinstance(e, Completed) for e in events)
    assert provider.open_streams == 0


def test_a_stream_never_started_and_closed_leaves_nothing_open() -> None:
    provider = FakeProvider()
    stream = provider.stream(_request())
    stream.close()
    assert provider.open_streams == 0


@pytest.mark.parametrize("after", [-1, True, 1.0])
def test_failure_after_deltas_is_a_whole_number(after: object) -> None:
    with pytest.raises(ValueError, match="failure_after_deltas"):
        FakeProvider(failure_after_deltas=after)  # type: ignore[arg-type]


def test_failure_must_be_an_error_kind() -> None:
    with pytest.raises(TypeError):
        FakeProvider(failure="timeout")  # type: ignore[arg-type]


@pytest.mark.parametrize("method", ["generate", "stream", "count_tokens"])
def test_only_an_llm_request_is_accepted(method: str) -> None:
    with pytest.raises(TypeError):
        getattr(FakeProvider(), method)({"task": "answer"})


def test_the_fake_never_opens_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "network access"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    provider = FakeProvider()
    req = _request()
    provider.generate(req)
    list(provider.stream(req))
    provider.count_tokens(req)
