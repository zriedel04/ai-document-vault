"""What is particular to the Anthropic adapter (``vault.ai.providers.anthropic``).

The shared contract is in ``test_llm_contract.py``. These tests read the requests the adapter
sends and drive every way the API and the connection answer, through recorded, synthetic
responses (``anthropic_replay``): no network, no real key.
"""

from __future__ import annotations

import contextlib
import json
import logging
import traceback
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from vault.ai.answer import ANSWER_SCHEMA
from vault.ai.answer_prompt import render_user_turn
from vault.ai.providers import anthropic as adapter
from vault.ai.providers import registry
from vault.ai.providers.anthropic import AnthropicProvider, error_kind
from vault.ai.providers.base import (
    Completed,
    Effort,
    ErrorKind,
    LLMError,
    LLMRequest,
    Message,
    Role,
    SourceBlock,
    StopReason,
    Task,
    TextDelta,
)

from .anthropic_replay import (
    RECORDINGS,
    REQUEST_ID,
    SYNTHETIC_KEY,
    NoRecording,
    Replay,
    SyntheticKey,
    error_body,
    sse_error,
)

MARKER = "zqxjv-private-marker"
MODEL = "claude-sonnet-5-5"
SYSTEM = "Answer only from the sources and cite each claim."


def _request(
    *,
    sources: int = 2,
    schema: bool = True,
    max_output_tokens: int = 2000,
    effort: Effort | None = None,
    task: Task = Task.ANSWER,
    messages: tuple[Message, ...] | None = None,
) -> LLMRequest:
    blocks = tuple(
        SourceBlock(
            label=f"S{n}",
            title=f"Synthetic syllabus {n}",
            text=f"The project milestone {n} is due on day {n * 7}. {MARKER} <b>&",
            page_start=n,
            page_end=n,
        )
        for n in range(1, sources + 1)
    )
    return LLMRequest(
        task=task,
        model=MODEL,
        system=SYSTEM,
        messages=messages or (Message(Role.USER, f"When is the milestone due? {MARKER}"),),
        sources=blocks,
        output_schema=ANSWER_SCHEMA if schema else None,
        max_output_tokens=max_output_tokens,
        effort=effort,
    )


def _provider(replay: Replay, **kwargs: Any) -> AnthropicProvider:
    return AnthropicProvider(SyntheticKey(), http_client=replay.client(), max_retries=0, **kwargs)


def _replace(old: str, new: str) -> Callable[[str], str]:
    return lambda text: text.replace(old, new)


def _walk(value: object) -> list[dict[str, object]]:
    """Every JSON object in ``value``, nested ones included."""
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        found.append(value)
        for item in value.values():
            found.extend(_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item))
    return found


# --- the request -----------------------------------------------------------------------------


def test_the_request_goes_to_the_messages_api_with_the_key() -> None:
    replay = Replay()
    _provider(replay).generate(_request())
    (sent,) = replay.requests
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    # The SDK sends a key as a bearer token from API version 2026-10-21 on.
    assert sent.headers["authorization"] == f"Bearer {SYNTHETIC_KEY}"
    assert sent.headers["anthropic-version"] == "2026-10-21"


def test_the_system_prompt_is_one_cached_block() -> None:
    replay = Replay()
    _provider(replay).generate(_request())
    assert replay.bodies[0]["system"] == [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]


def test_the_user_turn_holds_the_escaped_sources_and_question() -> None:
    replay = Replay()
    req = _request()
    _provider(replay).generate(req)
    messages = replay.bodies[0]["messages"]
    assert messages == [{"role": "user", "content": render_user_turn(req)}]
    content = messages[0]["content"]
    assert '<source id="S2" doc="Synthetic syllabus 2" page="2">' in content
    assert "&lt;b&gt;&amp;" in content
    assert "<b>" not in content


def test_earlier_turns_are_sent_as_they_are() -> None:
    replay = Replay()
    turns = (
        Message(Role.USER, "First question"),
        Message(Role.ASSISTANT, "First answer"),
        Message(Role.USER, "Second question"),
    )
    req = _request(messages=turns)
    _provider(replay).generate(req)
    assert replay.bodies[0]["messages"] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": render_user_turn(req)},
    ]


def test_a_task_without_sources_that_is_not_an_answer_sends_the_message_text() -> None:
    replay = Replay(override="messages.text.s2.sse")
    _provider(replay).generate(_request(sources=0, schema=False, task=Task.QUERY_REWRITE))
    assert replay.bodies[0]["messages"] == [
        {"role": "user", "content": f"When is the milestone due? {MARKER}"}
    ]


def test_an_answer_without_sources_still_uses_the_answer_turn() -> None:
    replay = Replay()
    req = _request(sources=0)
    _provider(replay).generate(req)
    assert replay.bodies[0]["messages"] == [{"role": "user", "content": render_user_turn(req)}]


def test_the_schema_is_sent_as_structured_output_in_the_form_the_api_accepts() -> None:
    replay = Replay()
    _provider(replay).generate(_request())
    output_config = replay.bodies[0]["output_config"]
    assert isinstance(output_config, dict)
    assert set(output_config) == {"format"}
    fmt = output_config["format"]
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    objects = [node for node in _walk(schema) if node.get("type") == "object"]
    assert len(objects) == 2  # the answer and its segment
    assert all(node["additionalProperties"] is False for node in objects)
    # The API refuses length and count bounds; they move into the description for the model.
    keys = {key for node in _walk(schema) for key in node}
    assert not keys & {"maxLength", "maxItems", "minLength"}
    descriptions = json.dumps(schema)
    assert "maxLength: 4000" in descriptions
    assert "maxItems: 100" in descriptions
    assert set(schema["required"]) == {"answer", "insufficient_evidence", "missing"}


def test_a_request_without_a_schema_sends_no_output_config() -> None:
    replay = Replay()
    _provider(replay).generate(_request(schema=False))
    assert "output_config" not in replay.bodies[0]


@pytest.mark.parametrize("effort", list(Effort))
def test_effort_is_sent_as_output_config_effort(effort: Effort) -> None:
    replay = Replay()
    _provider(replay).generate(_request(schema=False, effort=effort))
    assert replay.bodies[0]["output_config"] == {"effort": effort.value}


def test_the_request_has_no_tools_thinking_or_sampling_settings() -> None:
    replay = Replay()
    _provider(replay).generate(_request())
    body = replay.bodies[0]
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config", "stream"}
    assert body["model"] == MODEL
    assert body["max_tokens"] == 2000
    assert body["stream"] is True


def test_a_schema_the_sdk_cannot_express_is_an_invalid_request_and_nothing_is_sent() -> None:
    replay = Replay()
    req = LLMRequest(
        task=Task.CLASSIFY,
        model=MODEL,
        system=SYSTEM,
        messages=(Message(Role.USER, "Classify this."),),
        output_schema={"type": "object", "properties": {"label": {}}},
        max_output_tokens=100,
    )
    with pytest.raises(LLMError) as raised:
        _provider(replay).generate(req)
    assert raised.value.kind is ErrorKind.INVALID_REQUEST
    assert replay.requests == []


def test_count_tokens_sends_the_same_input_without_max_tokens() -> None:
    replay = Replay()
    provider = _provider(replay)
    assert provider.count_tokens(_request()) == 2042
    (sent,) = replay.requests
    assert sent.url.path == "/v1/messages/count_tokens"
    body = replay.bodies[0]
    assert "max_tokens" not in body
    assert body["system"] == [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    assert "format" in body["output_config"]  # type: ignore[operator]


# --- the response ----------------------------------------------------------------------------


def test_usage_counts_cached_and_written_tokens_as_input() -> None:
    response = _provider(Replay()).generate(_request())
    # messages.answer.s2.sse: 212 uncached, 0 written, 1830 read from the cache, 118 out.
    assert response.usage.input == 212 + 0 + 1830
    assert response.usage.cached == 1830
    assert response.usage.output == 118


def test_tokens_written_to_the_cache_are_input_and_not_cached() -> None:
    response = _provider(Replay()).generate(_request(sources=0))
    # messages.answer.s0.sse: 96 uncached, 1830 written, 0 read.
    assert response.usage.input == 96 + 1830
    assert response.usage.cached == 0


def test_a_response_without_cache_fields_has_no_cached_tokens() -> None:
    response = _provider(Replay()).generate(_request(schema=False))
    assert response.usage.input == 1650
    assert response.usage.cached == 0
    assert response.usage.output == 16


def test_the_answer_is_the_text_blocks_and_thinking_is_left_out() -> None:
    response = _provider(Replay()).generate(_request())
    assert response.text.startswith('{"answer": [')
    assert response.stop_reason is StopReason.COMPLETE
    assert response.structured is not None
    assert response.structured["insufficient_evidence"] is False
    assert "c3ludGhldGlj" not in response.text  # the thinking block's signature


def test_the_request_id_header_is_the_provider_request_id() -> None:
    response = _provider(Replay()).generate(_request())
    assert response.provider_request_id == REQUEST_ID
    assert response.latency_ms >= 0


@pytest.mark.parametrize(
    "header", ["", "req_", "msg_01abc", "req_abc\n", "req_abc def", "req_" + "a" * 129, MARKER]
)
def test_a_request_id_of_another_shape_is_dropped(header: str) -> None:
    response = _provider(Replay(request_id=header)).generate(_request())
    assert response.provider_request_id is None


def test_a_refusal_is_refused_with_no_structured_output() -> None:
    replay = Replay(override="messages.refusal.sse")
    response = _provider(replay).generate(_request())
    assert response.stop_reason is StopReason.REFUSED
    assert response.structured is None


def test_a_context_window_stop_is_a_cut_answer() -> None:
    edit = _replace('"stop_reason":"end_turn"', '"stop_reason":"model_context_window_exceeded"')
    response = _provider(Replay(edit=edit)).generate(_request())
    assert response.stop_reason is StopReason.MAX_TOKENS
    assert response.structured is None


def test_a_stop_sequence_is_a_complete_answer() -> None:
    edit = _replace('"stop_reason":"end_turn"', '"stop_reason":"stop_sequence"')
    response = _provider(Replay(edit=edit)).generate(_request())
    assert response.stop_reason is StopReason.COMPLETE
    assert response.structured is not None


BAD_EDITS = {
    "tool use stop": _replace('"stop_reason":"end_turn"', '"stop_reason":"tool_use"'),
    "pause stop": _replace('"stop_reason":"end_turn"', '"stop_reason":"pause_turn"'),
    "unknown stop": _replace('"stop_reason":"end_turn"', '"stop_reason":"elsewhere"'),
    "no stop": _replace('"stop_reason":"end_turn"', '"stop_reason":null'),
    "tool block": _replace(
        '"content_block":{"type":"thinking","thinking":"","signature":""}',
        '"content_block":{"type":"tool_use","id":"toolu_01","name":"x","input":{}}',
    ),
    "negative input": _replace('"input_tokens":212', '"input_tokens":-1'),
    "no input": _replace('"input_tokens":212', '"input_tokens":null'),
    "negative cache read": _replace(
        '"cache_read_input_tokens":1830', '"cache_read_input_tokens":-5'
    ),
    "more output than asked": _replace('"output_tokens":118', '"output_tokens":2001'),
    "fractional output": _replace('"output_tokens":118', '"output_tokens":117.5'),
    "answer not json": _replace('"missing\\": null}', '"missing\\": null'),
    "answer an array": _replace('{\\"answer\\": [{', "[{"),
}


@pytest.mark.parametrize("name", sorted(BAD_EDITS))
def test_an_answer_outside_the_contract_is_a_bad_response(name: str) -> None:
    replay = Replay(edit=BAD_EDITS[name])
    with pytest.raises(LLMError) as raised:
        _provider(replay).generate(_request())
    assert raised.value.kind is ErrorKind.BAD_RESPONSE
    assert replay.closed == replay.opened


def test_every_bad_edit_changes_the_recording() -> None:
    recorded = (RECORDINGS / "messages.answer.s2.sse").read_text(encoding="utf-8")
    for name, edit in BAD_EDITS.items():
        assert edit(recorded) != recorded, name


def test_the_text_must_be_the_streamed_text() -> None:
    message = SimpleNamespace(
        content=[anthropic.types.TextBlock(type="text", text="the final text")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    with pytest.raises(LLMError) as raised:
        adapter._response(_request(schema=False), message, "other text", None, 0)
    assert raised.value.kind is ErrorKind.BAD_RESPONSE
    ok = adapter._response(_request(schema=False), message, "the final text", None, 0)
    assert ok.text == "the final text"


def test_content_that_is_not_a_list_is_a_bad_response() -> None:
    message = SimpleNamespace(
        content=None,
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    with pytest.raises(LLMError) as raised:
        adapter._response(_request(schema=False), message, "", None, 0)
    assert raised.value.kind is ErrorKind.BAD_RESPONSE


def test_a_redacted_thinking_block_is_left_out() -> None:
    message = SimpleNamespace(
        content=[
            anthropic.types.RedactedThinkingBlock(type="redacted_thinking", data="c3ludA=="),
            anthropic.types.TextBlock(type="text", text="Day 7."),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=3),
    )
    response = adapter._response(_request(schema=False), message, "Day 7.", None, 0)
    assert response.text == "Day 7."


@pytest.mark.parametrize(
    "body", ['{"input_tokens": 1.5}', '{"input_tokens": -1}', '{"input_tokens": null}', "{}"]
)
def test_a_token_count_of_the_wrong_shape_is_a_bad_response(body: str) -> None:
    replay = Replay(edit=lambda _text: body)
    with pytest.raises(LLMError) as raised:
        _provider(replay).count_tokens(_request())
    assert raised.value.kind is ErrorKind.BAD_RESPONSE


# --- streaming -------------------------------------------------------------------------------


def test_the_stream_yields_each_recorded_text_delta() -> None:
    events = list(_provider(Replay()).stream(_request()))
    deltas = [event.text for event in events if isinstance(event, TextDelta)]
    assert len(deltas) == 6  # messages.answer.s2.sse has six text deltas
    assert isinstance(events[-1], Completed)
    assert "".join(deltas) == events[-1].response.text


@pytest.mark.parametrize("how", ["close", "closing", "finish", "fail"])
def test_every_way_a_stream_ends_closes_the_http_response(how: str) -> None:
    replay = Replay(failure=ErrorKind.UNAVAILABLE, after=2) if how == "fail" else Replay()
    provider = _provider(replay)
    if how == "close":
        stream = provider.stream(_request())
        assert isinstance(next(stream), TextDelta)
        assert replay.closed == 0
        stream.close()
    elif how == "closing":
        with contextlib.closing(provider.stream(_request())) as stream:
            next(stream)
    elif how == "finish":
        list(provider.stream(_request()))
    else:
        with pytest.raises(LLMError):
            list(provider.stream(_request()))
    assert replay.opened == 1
    assert replay.closed == 1
    assert provider.open_streams == 0


def test_a_dropped_connection_mid_stream_is_unavailable_after_the_deltas() -> None:
    replay = Replay(drop_after=2)
    received: list[object] = []
    with pytest.raises(LLMError) as raised:
        received.extend(_provider(replay).stream(_request()))
    assert raised.value.kind is ErrorKind.UNAVAILABLE
    assert raised.value.retryable
    assert len(received) == 2
    assert replay.closed == 1


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_every_kind_can_arrive_mid_stream(kind: ErrorKind) -> None:
    replay = Replay(failure=kind, after=3)
    received: list[object] = []
    with pytest.raises(LLMError) as raised:
        received.extend(_provider(replay).stream(_request()))
    assert raised.value.kind is kind
    assert len(received) == 3
    assert all(isinstance(event, TextDelta) for event in received)


# --- errors ----------------------------------------------------------------------------------

# Every recorded error type, with the status the API sends it with and the kind it must become.
ERROR_TYPES = {
    "authentication_error": (401, ErrorKind.AUTHENTICATION),
    "permission_error": (403, ErrorKind.AUTHENTICATION),
    "billing_error": (402, ErrorKind.AUTHENTICATION),
    "invalid_request_error": (400, ErrorKind.INVALID_REQUEST),
    "not_found_error": (404, ErrorKind.INVALID_REQUEST),
    "request_too_large": (413, ErrorKind.INVALID_REQUEST),
    "rate_limit_error": (429, ErrorKind.RATE_LIMITED),
    "api_error": (500, ErrorKind.UNAVAILABLE),
    "overloaded_error": (529, ErrorKind.UNAVAILABLE),
    "timeout_error": (504, ErrorKind.TIMEOUT),
}


def test_every_recorded_error_type_is_in_the_table_and_the_adapter() -> None:
    recorded = {path.name[len("error.") : -len(".json")] for path in RECORDINGS.glob("error.*")}
    assert recorded == set(ERROR_TYPES)
    assert set(adapter._ERROR_TYPES) == set(ERROR_TYPES)


def _answering(status: int, body: bytes, headers: dict[str, str] | None = None) -> httpx2.Client:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, headers=headers or {}, content=body, request=request)

    return httpx2.Client(transport=httpx2.MockTransport(handle))


def _sdk_error(
    status: int,
    body: bytes,
    call: Callable[[AnthropicProvider], object] = lambda p: p.generate(_request()),
) -> LLMError:
    provider = AnthropicProvider(
        SyntheticKey(), http_client=_answering(status, body), max_retries=0
    )
    with pytest.raises(LLMError) as raised:
        call(provider)
    return raised.value


@pytest.mark.parametrize("error_type", sorted(ERROR_TYPES))
def test_every_error_type_maps_to_its_kind(error_type: str) -> None:
    status, kind = ERROR_TYPES[error_type]
    assert _sdk_error(status, error_body(error_type)).kind is kind
    counted = _sdk_error(status, error_body(error_type), lambda p: p.count_tokens(_request()))
    assert counted.kind is kind


@pytest.mark.parametrize("error_type", sorted(ERROR_TYPES))
def test_an_error_event_in_a_200_stream_maps_by_its_type(error_type: str) -> None:
    _, kind = ERROR_TYPES[error_type]
    body = sse_error(error_type).encode("utf-8")
    assert _sdk_error(200, body, lambda p: list(p.stream(_request()))).kind is kind


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, ErrorKind.INVALID_REQUEST),
        (401, ErrorKind.AUTHENTICATION),
        (402, ErrorKind.AUTHENTICATION),
        (403, ErrorKind.AUTHENTICATION),
        (404, ErrorKind.INVALID_REQUEST),
        (408, ErrorKind.TIMEOUT),
        (409, ErrorKind.UNAVAILABLE),
        (413, ErrorKind.INVALID_REQUEST),
        (418, ErrorKind.INVALID_REQUEST),
        (422, ErrorKind.INVALID_REQUEST),
        (429, ErrorKind.RATE_LIMITED),
        (500, ErrorKind.UNAVAILABLE),
        (503, ErrorKind.UNAVAILABLE),
        (529, ErrorKind.UNAVAILABLE),
    ],
)
def test_an_error_without_a_known_type_maps_by_its_status(status: int, kind: ErrorKind) -> None:
    body = json.dumps({"type": "error", "error": {"type": "new_error", "message": MARKER}})
    assert _sdk_error(status, body.encode("utf-8")).kind is kind
    assert _sdk_error(status, b"<html>not json</html>").kind is kind


def test_an_unknown_error_event_in_a_200_stream_is_a_bad_response() -> None:
    body = 'event: error\ndata: {"type": "error", "error": {"type": "new_error"}}\n\n'
    error = _sdk_error(200, body.encode("utf-8"), lambda p: list(p.stream(_request())))
    assert error.kind is ErrorKind.BAD_RESPONSE


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (httpx2.ConnectError("synthetic"), ErrorKind.UNAVAILABLE),
        (httpx2.ConnectTimeout("synthetic"), ErrorKind.TIMEOUT),
        (httpx2.ReadTimeout("synthetic"), ErrorKind.TIMEOUT),
        (httpx2.RemoteProtocolError("synthetic"), ErrorKind.UNAVAILABLE),
    ],
)
def test_transport_failures_before_an_answer(raised: Exception, kind: ErrorKind) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise raised

    client = httpx2.Client(transport=httpx2.MockTransport(handle))
    provider = AnthropicProvider(SyntheticKey(), http_client=client, max_retries=0)
    for call in (provider.generate, provider.count_tokens, lambda r: list(provider.stream(r))):
        with pytest.raises(LLMError) as caught:
            call(_request())
        assert caught.value.kind is kind


def test_error_kind_leaves_other_exceptions_alone() -> None:
    assert error_kind(RuntimeError("x")) is None
    assert error_kind(KeyError("x")) is None
    assert error_kind(anthropic.AnthropicError("client side")) is ErrorKind.INVALID_REQUEST


def test_a_programming_error_inside_the_sdk_propagates() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise NoRecording

    client = httpx2.Client(transport=httpx2.MockTransport(handle))
    provider = AnthropicProvider(SyntheticKey(), http_client=client, max_retries=0)
    with pytest.raises(NoRecording):
        provider.generate(_request())
    assert provider.open_streams == 0


def test_a_failed_call_is_retried_by_the_sdk() -> None:
    answers = [
        httpx2.Response(
            529,
            headers={"retry-after-ms": "1"},
            content=error_body("overloaded_error"),
        )
    ]
    replay = Replay()

    def handle(request: httpx2.Request) -> httpx2.Response:
        return answers.pop() if answers else replay.handle(request)

    client = httpx2.Client(transport=httpx2.MockTransport(handle))
    provider = AnthropicProvider(SyntheticKey(), http_client=client, max_retries=1)
    assert provider.generate(_request()).stop_reason is StopReason.COMPLETE
    assert answers == []


@pytest.mark.parametrize("error_type", sorted(ERROR_TYPES))
def test_an_error_carries_nothing_from_the_request_the_answer_or_the_key(error_type: str) -> None:
    status, _ = ERROR_TYPES[error_type]
    body = json.loads(error_body(error_type))
    body["error"]["message"] = f"{body['error']['message']} {MARKER}"
    error = _sdk_error(status, json.dumps(body).encode("utf-8"))
    assert error.__cause__ is None
    assert error.__context__ is None
    shown = "".join(traceback.format_exception(error)) + repr(error) + str(error)
    for secret in (MARKER, SYNTHETIC_KEY, "req_011SynthErrorRecord", error_type):
        assert secret not in shown


# --- the key ---------------------------------------------------------------------------------


def test_without_a_key_every_call_is_an_authentication_error_and_nothing_is_sent() -> None:
    replay = Replay()
    provider = AnthropicProvider(None, http_client=replay.client())
    for call in (provider.generate, provider.count_tokens, lambda r: list(provider.stream(r))):
        with pytest.raises(LLMError) as raised:
            call(_request())
        assert raised.value.kind is ErrorKind.AUTHENTICATION
    assert replay.requests == []
    assert provider.open_streams == 0


@pytest.mark.parametrize(
    "value", ["", " ", "sk-ant key", "sk-ant\n", "sk-ant\r", "sk-ant\t", "sk-é", "x" * 513]
)
def test_a_key_that_cannot_be_a_header_is_refused_without_repeating_it(value: str) -> None:
    with pytest.raises(ValueError, match="visible ASCII") as raised:
        AnthropicProvider(SyntheticKey(value))
    assert value.strip() == "" or value not in str(raised.value)


class _Plain:
    def __init__(self, value: str) -> None:
        self.value = value

    def reveal(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"_Plain({self.value})"


def test_a_key_object_that_shows_its_text_is_refused() -> None:
    with pytest.raises(ValueError, match="hide its text") as raised:
        AnthropicProvider(_Plain(SYNTHETIC_KEY))
    assert SYNTHETIC_KEY not in str(raised.value)


def test_an_object_without_reveal_is_refused() -> None:
    with pytest.raises(ValueError, match="visible ASCII"):
        AnthropicProvider(object())  # type: ignore[arg-type]


def test_the_provider_repr_shows_no_key() -> None:
    provider = AnthropicProvider(SyntheticKey())
    assert repr(provider) == "AnthropicProvider()"
    assert SYNTHETIC_KEY not in repr(provider)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"max_retries": -1}, ValueError),
        ({"max_retries": 11}, ValueError),
        ({"max_retries": True}, ValueError),
        ({"max_retries": 1.0}, ValueError),
        ({"timeout_seconds": 0}, ValueError),
        ({"timeout_seconds": 3601}, ValueError),
        ({"timeout_seconds": True}, ValueError),
        ({"timeout_seconds": "60"}, ValueError),
        ({"http_client": object()}, TypeError),
    ],
)
def test_settings_are_checked(kwargs: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        AnthropicProvider(SyntheticKey(), **kwargs)


def test_the_environment_does_not_redirect_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://elsewhere.invalid")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-environment")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-the-environment")
    replay = Replay()
    _provider(replay).generate(_request())
    (sent,) = replay.requests
    assert sent.url.host == "api.anthropic.com"
    assert sent.headers.get_list("authorization") == [f"Bearer {SYNTHETIC_KEY}"]


def test_the_registry_creates_the_anthropic_adapter() -> None:
    assert isinstance(registry.create_provider("anthropic"), AnthropicProvider)


def test_the_registry_creates_an_adapter_without_a_key_that_sends_nothing() -> None:
    provider = registry.create_provider("anthropic")
    with pytest.raises(LLMError) as raised:
        provider.generate(_request())
    assert raised.value.kind is ErrorKind.AUTHENTICATION


# --- logs ------------------------------------------------------------------------------------


def _enable_sdk_logging() -> None:
    """What ``ANTHROPIC_LOG=debug`` does when the SDK is imported."""
    for name in adapter.SDK_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = True


def test_the_sdk_would_log_the_request_body_unless_silenced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The control: with its loggers on, the SDK writes the prompt into a DEBUG record."""
    caplog.set_level(logging.DEBUG)
    _enable_sdk_logging()
    try:
        client = anthropic.Anthropic(
            api_key=SYNTHETIC_KEY, http_client=Replay().client(), max_retries=0
        )
        with client.messages.stream(
            model=MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": f"<source id=x>{MARKER}"}],
        ) as stream:
            stream.until_done()
    except NoRecording:
        pass
    finally:
        adapter.silence_sdk_loggers()
    assert any(MARKER in record.getMessage() for record in caplog.records)


def test_no_sdk_record_reaches_a_handler_even_after_anthropic_log_turned_them_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _enable_sdk_logging()
    provider = _provider(Replay())
    provider.generate(_request(schema=False))
    provider.count_tokens(_request())
    with pytest.raises(LLMError):
        _provider(Replay(drop_after=2)).generate(_request())
    names = {record.name.split(".")[0] for record in caplog.records}
    assert not names & set(adapter.SDK_LOGGERS)
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert SYNTHETIC_KEY not in record.getMessage()


def test_silencing_is_idempotent() -> None:
    adapter.silence_sdk_loggers()
    adapter.silence_sdk_loggers()
    for name in adapter.SDK_LOGGERS:
        logger = logging.getLogger(name)
        nulls = [h for h in logger.handlers if type(h) is logging.NullHandler]
        assert len(nulls) == 1
        assert not logger.isEnabledFor(logging.CRITICAL)
        assert logger.propagate is False
