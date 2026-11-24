"""The Anthropic adapter: :class:`LLMProvider` over the Messages API (plan section 9, ADR 0009).

It translates an :class:`LLMRequest` for the official ``anthropic`` SDK and the SDK's answer back,
and is, with the other adapters in this package, the only code that imports a provider SDK.

- **Key.** The constructor takes the key as an object with ``reveal()`` whose ``repr`` hides it
  (``vault.ai.keys.ApiKey`` once the key store exists). This module reads no environment variable
  and no Credential Manager entry for it; without a key every call is
  :attr:`ErrorKind.AUTHENTICATION` and nothing is sent. The SDK is given the key, the API's
  address and ``open_telemetry=False`` explicitly, so ``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``,
  a profile on disk and OpenTelemetry's content capture cannot change where a request goes or
  copy it elsewhere. (The SDK still adds the headers in ``ANTHROPIC_CUSTOM_HEADERS`` and httpx2
  uses the system's proxy settings; neither changes the address or the body.)
- **Request.** ``system`` is one text block with ``cache_control`` (prompt caching: the prompt is
  identical for every question, so a follow-up reads it from the cache). Earlier turns are sent as
  they are; the last user turn is :func:`vault.ai.answer_prompt.render_user_turn` (the escaped
  ``<sources>`` and ``<question>`` blocks) when the request has sources or is an answer task, and
  the message text otherwise. ``output_schema`` becomes ``output_config.format`` (structured
  output), after the SDK's own ``transform_schema``, which moves what the API does not accept
  (``maxLength``, ``maxItems``) into descriptions: the consumer still checks the whole schema.
  ``effort`` becomes ``output_config.effort``; with none the model's default applies. No
  ``thinking`` setting is sent (each current model's default) and no tools: the model has no
  capabilities (ADR 0010). Server-side refusal fallbacks are not used either: a refusal is
  :attr:`StopReason.REFUSED`, and the model that answered is always the one the route named.
- **Streaming.** ``generate`` and ``stream`` both stream (the SDK's ``messages.stream``), so a long
  answer meets no request timeout and both give the same response; ``generate`` keeps only the
  last event. Text deltas are yielded as they arrive; thinking blocks are not text and are left
  out. The joined deltas must equal the final text, or the answer is :attr:`ErrorKind.BAD_RESPONSE`.
  Closing the stream leaves the SDK's ``with`` block, which closes the HTTP response.
- **Response.** ``usage.input`` is every input token the API reports (uncached, written to the
  cache and read from it), ``usage.cached`` the tokens read from the cache, ``usage.output`` the
  output tokens (thinking included). ``end_turn`` and ``stop_sequence`` are
  :attr:`StopReason.COMPLETE`, ``max_tokens`` and ``model_context_window_exceeded``
  :attr:`StopReason.MAX_TOKENS`, ``refusal`` :attr:`StopReason.REFUSED`; any other stop reason, a
  content block other than text or thinking (no tools were offered), or counts that are not whole
  numbers within the request's bounds are :attr:`ErrorKind.BAD_RESPONSE`. The counts are read
  from the SDK's typed message, whose lax validation already turns a JSON ``"212"`` or ``true``
  into an ``int``; ``null``, a fraction, a negative number or more output tokens than asked is
  refused. ``provider_request_id`` is the ``request-id`` header when it has the API's shape, else
  ``None``.
- **Errors.** Every SDK and transport error becomes one of the six :class:`ErrorKind` values, by the
  API's error type when the body names one (``overloaded_error`` mid-stream arrives with status
  200) and by status code otherwise; see :func:`error_kind`. The :class:`LLMError` is raised after
  the ``except`` block, so it has neither a cause nor a context: the SDK's message, which quotes
  the response body, and the request id never travel with it. The SDK retries 408, 409, 429, 5xx
  and connection errors itself (``max_retries``, 2 by default) before one is reported.
- **Logs.** The SDK logs the whole request body at DEBUG (``Request options``) and ``ANTHROPIC_LOG``
  turns that on. Before each call the ``anthropic``, ``httpx2`` and ``httpcore2`` loggers are
  silenced (:func:`silence_sdk_loggers`): no level is low enough for them to make a record, and a
  record a child logger makes anyway stops there and reaches no handler.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import Generator, Mapping
from typing import Any, Final, Protocol

import anthropic
import httpx2

from vault.ai.answer_prompt import render_user_turn
from vault.ai.providers.base import (
    Completed,
    ErrorKind,
    LLMError,
    LLMEvent,
    LLMRequest,
    LLMResponse,
    StopReason,
    Task,
    TextDelta,
    Usage,
    parse_structured,
)

BASE_URL: Final = "https://api.anthropic.com"
DEFAULT_MAX_RETRIES: Final = 2
DEFAULT_TIMEOUT_SECONDS: Final = 600.0
SDK_LOGGERS: Final = ("anthropic", "httpx2", "httpcore2")
# Visible ASCII only: a key with a line break or a space could not be a header value, and httpx2's
# error for one would repeat the key.
_KEY_SHAPE: Final = re.compile(r"[\x21-\x7e]{1,512}")
_REQUEST_ID: Final = re.compile(r"req_[A-Za-z0-9]{1,128}")
_ANSWER_TASKS: Final = frozenset({Task.ANSWER, Task.ANSWER_DEEP})

_STOP_REASONS: Final = {
    "end_turn": StopReason.COMPLETE,
    "stop_sequence": StopReason.COMPLETE,
    "max_tokens": StopReason.MAX_TOKENS,
    "model_context_window_exceeded": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSED,
}
# Blocks that are not the answer's text and may appear without tools.
_SILENT_BLOCKS: Final = frozenset({"thinking", "redacted_thinking"})

# The API's error types (``error.type`` in an error body or an SSE ``error`` event).
_ERROR_TYPES: Final = {
    "authentication_error": ErrorKind.AUTHENTICATION,
    "permission_error": ErrorKind.AUTHENTICATION,
    "billing_error": ErrorKind.AUTHENTICATION,
    "invalid_request_error": ErrorKind.INVALID_REQUEST,
    "not_found_error": ErrorKind.INVALID_REQUEST,
    "request_too_large": ErrorKind.INVALID_REQUEST,
    "rate_limit_error": ErrorKind.RATE_LIMITED,
    "api_error": ErrorKind.UNAVAILABLE,
    "overloaded_error": ErrorKind.UNAVAILABLE,
    "timeout_error": ErrorKind.TIMEOUT,
}


class SecretKey(Protocol):
    """An API key that shows its text only through :meth:`reveal` (``vault.ai.keys.ApiKey``)."""

    def reveal(self) -> str: ...


def silence_sdk_loggers() -> None:
    """Make the SDK's and httpx2's loggers write nothing, whatever ``ANTHROPIC_LOG`` set."""
    for name in SDK_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False
        if not any(type(handler) is logging.NullHandler for handler in logger.handlers):
            logger.addHandler(logging.NullHandler())


silence_sdk_loggers()


def _status_kind(status: int) -> ErrorKind:
    if status in {401, 402, 403}:
        return ErrorKind.AUTHENTICATION
    if status == 408:
        return ErrorKind.TIMEOUT
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status == 409 or status >= 500:
        return ErrorKind.UNAVAILABLE
    if 400 <= status < 500:
        return ErrorKind.INVALID_REQUEST
    return ErrorKind.BAD_RESPONSE  # an error event in a 200 stream with no known type


def error_kind(exc: BaseException) -> ErrorKind | None:
    """The :class:`ErrorKind` of an exception the SDK or httpx2 raised, or ``None`` if it is not
    one of theirs (a programming error, which propagates)."""
    if isinstance(exc, anthropic.APITimeoutError | httpx2.TimeoutException):
        return ErrorKind.TIMEOUT
    if isinstance(exc, anthropic.APIConnectionError | httpx2.HTTPError):
        return ErrorKind.UNAVAILABLE
    if isinstance(exc, anthropic.APIStatusError):
        named = _ERROR_TYPES.get(exc.type) if isinstance(exc.type, str) else None
        return named if named is not None else _status_kind(exc.status_code)
    if isinstance(exc, anthropic.APIResponseValidationError):
        return ErrorKind.BAD_RESPONSE
    if isinstance(exc, anthropic.AnthropicError):
        return ErrorKind.INVALID_REQUEST  # refused by the SDK before sending
    if isinstance(exc, ValueError):
        return ErrorKind.BAD_RESPONSE  # an event that is not JSON, or JSON of the wrong shape
    return None


def _whole(value: object, *, optional: bool = False) -> int | None:
    """``value`` if it is a whole number of at least 0; ``0`` for an absent optional count."""
    if value is None and optional:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage(message: Any, req: LLMRequest) -> Usage:
    usage = getattr(message, "usage", None)
    uncached = _whole(getattr(usage, "input_tokens", None))
    written = _whole(getattr(usage, "cache_creation_input_tokens", None), optional=True)
    read = _whole(getattr(usage, "cache_read_input_tokens", None), optional=True)
    output = _whole(getattr(usage, "output_tokens", None))
    if uncached is None or written is None or read is None or output is None:
        raise LLMError(ErrorKind.BAD_RESPONSE)
    if output > req.max_output_tokens:
        raise LLMError(ErrorKind.BAD_RESPONSE)
    return Usage(input=uncached + written + read, output=output, cached=read)


def _text(message: Any) -> str:
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        raise LLMError(ErrorKind.BAD_RESPONSE)
    pieces = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "text" and isinstance(getattr(block, "text", None), str):
            pieces.append(block.text)
        elif kind not in _SILENT_BLOCKS:
            raise LLMError(ErrorKind.BAD_RESPONSE)
    return "".join(pieces)


def _request_id(value: object) -> str | None:
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else None


class AnthropicProvider:
    """The Anthropic Messages API behind :class:`LLMProvider`. See the module docstring.

    ``http_client`` replaces the SDK's own HTTP client (tests pass one with an
    ``httpx2.MockTransport`` that replays recorded responses); ``max_retries`` and
    ``timeout_seconds`` are the SDK's settings.
    """

    __slots__ = ("_client", "_lock", "_open")
    name: str = "anthropic"

    def __init__(
        self,
        api_key: SecretKey | None = None,
        *,
        http_client: httpx2.Client | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            msg = "max_retries must be a whole number from 0 to 10"
            raise ValueError(msg)
        if not 0 <= max_retries <= 10:
            msg = "max_retries must be a whole number from 0 to 10"
            raise ValueError(msg)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 3600
        ):
            msg = "timeout_seconds must be a number above 0 and at most 3600"
            raise ValueError(msg)
        if http_client is not None and not isinstance(http_client, httpx2.Client):
            msg = "http_client must be an httpx2.Client"
            raise TypeError(msg)
        self._lock = threading.Lock()
        self._open = 0
        self._client: anthropic.Anthropic | None = None
        if api_key is None:
            return
        secret = api_key.reveal() if callable(getattr(api_key, "reveal", None)) else None
        if not isinstance(secret, str) or not _KEY_SHAPE.fullmatch(secret):
            msg = "the API key must be visible ASCII characters with no spaces"
            raise ValueError(msg)
        if secret in repr(api_key) or secret in str(api_key):
            msg = "the API key object must hide its text in repr and str"
            raise ValueError(msg)
        silence_sdk_loggers()
        self._client = anthropic.Anthropic(
            api_key=secret,
            base_url=BASE_URL,
            max_retries=max_retries,
            timeout=float(timeout_seconds),
            http_client=http_client,
            open_telemetry=False,
        )

    def __repr__(self) -> str:
        return "AnthropicProvider()"

    @property
    def open_streams(self) -> int:
        """Streams started and not yet ended (finished, failed or closed)."""
        with self._lock:
            return self._open

    def generate(self, req: LLMRequest) -> LLMResponse:
        _check(req)
        with contextlib.closing(self._stream(req)) as events:
            for event in events:
                if isinstance(event, Completed):
                    return event.response
        raise LLMError(ErrorKind.BAD_RESPONSE)  # pragma: no cover - _stream ends with Completed

    def stream(self, req: LLMRequest) -> Generator[LLMEvent, None, None]:
        _check(req)
        return self._stream(req)

    def count_tokens(self, req: LLMRequest) -> int:
        _check(req)
        client = self._ready()
        params = _params(req)
        del params["max_tokens"]
        kind: ErrorKind | None = None
        count: object = None
        try:
            result = client.messages.count_tokens(**params)
            count = getattr(result, "input_tokens", None)
        except Exception as exc:
            kind = _mapped(exc)
        if kind is not None:
            raise LLMError(kind)
        whole = _whole(count)
        if whole is None:
            raise LLMError(ErrorKind.BAD_RESPONSE)
        return whole

    def _ready(self) -> anthropic.Anthropic:
        if self._client is None:
            raise LLMError(ErrorKind.AUTHENTICATION)
        silence_sdk_loggers()
        return self._client

    def _stream(self, req: LLMRequest) -> Generator[LLMEvent, None, None]:
        with self._lock:
            self._open += 1
        try:
            client = self._ready()
            params = _params(req)
            started = time.perf_counter_ns()
            sent: list[str] = []
            kind: ErrorKind | None = None
            final: Any = None
            request_id: object = None
            try:
                with client.messages.stream(**params) as events:
                    request_id = events.request_id
                    for event in events:
                        if event.type == "text" and event.text:
                            sent.append(event.text)
                            yield TextDelta(event.text)
                    final = events.get_final_message()
            except Exception as exc:
                kind = _mapped(exc)
            if kind is not None:
                raise LLMError(kind)
            latency_ms = (time.perf_counter_ns() - started) // 1_000_000
            yield Completed(_response(req, final, "".join(sent), request_id, latency_ms))
        finally:
            with self._lock:
                self._open -= 1


def _check(req: object) -> None:
    if not isinstance(req, LLMRequest):
        msg = "the request must be an LLMRequest"
        raise TypeError(msg)


def _mapped(exc: Exception) -> ErrorKind:
    """The kind of an SDK error; anything else is re-raised as it is (a programming error)."""
    kind = error_kind(exc)
    if kind is None:
        raise exc
    return kind


def _params(req: LLMRequest) -> dict[str, Any]:
    """The SDK's keyword arguments for ``req`` (see the module docstring)."""
    messages: list[dict[str, str]] = [
        {"role": message.role.value, "content": message.content} for message in req.messages
    ]
    if req.sources or req.task in _ANSWER_TASKS:
        messages[-1] = {"role": "user", "content": render_user_turn(req)}
    params: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_output_tokens,
        "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
    }
    output_config: dict[str, object] = {}
    schema = req.schema()
    if schema is not None:
        try:
            output_config["format"] = {
                "type": "json_schema",
                "schema": anthropic.transform_schema(schema),
            }
        except (TypeError, ValueError):
            raise LLMError(ErrorKind.INVALID_REQUEST) from None
    if req.effort is not None:
        output_config["effort"] = req.effort.value
    if output_config:
        params["output_config"] = output_config
    return params


def _response(
    req: LLMRequest, message: Any, streamed: str, request_id: object, latency_ms: int
) -> LLMResponse:
    stop = _STOP_REASONS.get(getattr(message, "stop_reason", None) or "")
    if stop is None:
        raise LLMError(ErrorKind.BAD_RESPONSE)
    text = _text(message)
    if text != streamed:
        raise LLMError(ErrorKind.BAD_RESPONSE)
    usage = _usage(message, req)
    structured: Mapping[str, Any] | None = None
    if req.output_schema is not None and stop is StopReason.COMPLETE:
        structured = parse_structured(text)
    return LLMResponse(
        text=text,
        structured=structured,
        usage=usage,
        stop_reason=stop,
        provider_request_id=_request_id(request_id),
        latency_ms=latency_ms,
    )
