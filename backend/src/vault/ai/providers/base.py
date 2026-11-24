"""The provider-neutral language model interface (plan section 9, ADR 0009).

Every feature asks the AI service for a *task*; the service builds an :class:`LLMRequest` and
hands it to one :class:`LLMProvider`. The types here are the whole contract between the service
and a provider adapter, so they are checked when they are built, not by each adapter:

- **Request.** ``task``, ``model``, ``system``, ``messages``, ``sources``, ``output_schema``,
  ``max_output_tokens`` and ``effort``, the fields section 9 names. ``sources`` are data
  (:class:`SourceBlock`), not a pre-formatted string, so an adapter can use a provider's native
  document features; their labels are ``S1`` to ``Sn`` in order (ADR 0011), and nothing in a
  request names a database id: the server alone maps a label to a chunk. Which chunks may be in
  ``sources`` at all is the egress gate's decision (ADR 0010), which comes with the AI service.
- **Response.** ``text``, ``structured``, ``usage`` (input, output and cached tokens, as the
  provider reports them), ``stop_reason``, ``provider_request_id`` and ``latency_ms``.
- **Structured output.** A request with ``output_schema`` gets ``structured``: the JSON object
  the model wrote, parsed by :func:`parse_structured` (one rule for every adapter), when the
  model finished; ``None`` when it was cut off or refused, since a cut JSON text is not an
  answer. Checking it against the schema belongs to the consumer (the citation validator reads
  the cited-answer shape in ``vault.ai.answer``), so a provider never "repairs" an answer.
- **Streaming.** :meth:`LLMProvider.stream` yields zero or more :class:`TextDelta` and then
  exactly one :class:`Completed` holding the same response :meth:`LLMProvider.generate` returns:
  the deltas joined are its ``text``. It is a generator so that it can be cancelled: ``close()``
  on it (or leaving a ``with contextlib.closing(...)`` block) stops it, an adapter closes its
  connection in a ``finally`` block, and nothing more is yielded.
- **Errors.** A provider failure is an :class:`LLMError` with an :class:`ErrorKind`, raised by the
  call or from the stream. Its message is fixed text per kind and never the provider's own
  message, which may quote the prompt; an adapter raises it ``from None``. A request that breaks
  the rules here is a programming error and raises ``ValueError`` when it is built, also with
  fixed text that never repeats a field's value.

No type here is logged as text: ``task``, ``stop_reason`` and an error's ``kind`` are enums (the
logger writes their names) and counts become :func:`vault.core.logging.metric`. Prompt, source and
answer text are never logged (ADR 0010).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Final, Protocol

# Provider model ids: ``claude-sonnet-5-5``, ``gpt-5.6-luna``, ``llama3.1:8b``. Checked with
# ``fullmatch`` (``$`` would accept a trailing newline).
MODEL_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
MAX_OUTPUT_TOKENS: Final = 128_000
# The providers a ``LLMProvider.name`` may be: the two of plan section 9 and the fake. The usage
# ledger (``ai_requests.provider``) and the price table (``[pricing]``) accept exactly these; a new
# adapter (a local model) adds its name here, with a migration for the ledger's CHECK.
PROVIDER_NAMES: Final = ("anthropic", "openai", "fake")


class Task(Enum):
    """What a feature asks for (plan section 9: features never name a model)."""

    ANSWER = "answer"
    ANSWER_DEEP = "answer_deep"
    CLASSIFY = "classify"
    QUERY_REWRITE = "query_rewrite"


class Role(Enum):
    USER = "user"
    ASSISTANT = "assistant"


class Effort(Enum):
    """How hard the model should think; ``None`` on a request leaves the provider's default."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StopReason(Enum):
    COMPLETE = "complete"  # the model finished
    MAX_TOKENS = "max_tokens"  # cut off at max_output_tokens
    REFUSED = "refused"  # the provider or the model declined to answer


class ErrorKind(Enum):
    AUTHENTICATION = "authentication"  # no key, a wrong key, or no access to the model
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"  # the provider is down, overloaded, or cannot be reached
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"  # refused as asked: too long, unknown model, bad schema
    BAD_RESPONSE = "bad_response"  # the provider answered with something the contract excludes


RETRYABLE: Final = frozenset({ErrorKind.RATE_LIMITED, ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT})

_ERROR_TEXT: Final = MappingProxyType(
    {
        ErrorKind.AUTHENTICATION: "the provider refused the API key or the model",
        ErrorKind.RATE_LIMITED: "the provider's rate limit was reached",
        ErrorKind.UNAVAILABLE: "the provider could not be reached or is unavailable",
        ErrorKind.TIMEOUT: "the provider did not answer in time",
        ErrorKind.INVALID_REQUEST: "the provider refused the request",
        ErrorKind.BAD_RESPONSE: "the provider's answer did not fit the contract",
    }
)


class LLMError(Exception):
    """A provider call that failed. The message is fixed text for its kind."""

    def __init__(self, kind: ErrorKind) -> None:
        if not isinstance(kind, ErrorKind):
            msg = "an LLMError needs an ErrorKind"
            raise TypeError(msg)
        super().__init__(_ERROR_TEXT[kind])
        self.kind = kind

    @property
    def retryable(self) -> bool:
        """Whether the same request may succeed later (rate limit, outage, timeout)."""
        return self.kind in RETRYABLE


def _text(value: object, what: str) -> str:
    """``value`` if it is a string holding a character other than white space."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{what} must be text that is not blank"
        raise ValueError(msg)
    return value


def _count(value: object, what: str, minimum: int, maximum: int | None = None) -> int:
    """``value`` if it is an ``int`` (not a ``bool``) within the bounds. ``minimum`` and
    ``maximum`` are constants of this module, never values from a request."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        msg = f"{what} must be a whole number of at least {minimum}"
        raise ValueError(msg)
    if maximum is not None and value > maximum:
        msg = f"{what} must be at most {maximum}"
        raise ValueError(msg)
    return value


def _member[E: Enum](value: object, kind: type[E], what: str) -> E:
    if not isinstance(value, kind):
        msg = f"{what} must be a {kind.__name__}"
        raise ValueError(msg)
    return value


type JSONValue = bool | int | float | str | tuple[JSONValue, ...] | Mapping[str, JSONValue] | None


def _freeze(value: object) -> JSONValue:
    """A read-only copy of a value ``json.loads`` produced: mappings and tuples all the way down."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    msg = "not a JSON value"  # unreachable for json.loads output; kept as a guard
    raise TypeError(msg)


def thaw(value: object) -> object:
    """A plain, independent ``dict``/``list`` copy of a frozen JSON value (for an SDK or parser)."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def _reject_constant(name: str) -> object:
    raise ValueError(name)


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):  # ``1e999`` parses to infinity
        raise ValueError(text)
    return number


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        msg = "duplicate key"
        raise ValueError(msg)
    return result


def _strict_json(text: str) -> object:
    """``json.loads`` without extensions: no NaN or Infinity, no infinite number, no key twice."""
    return json.loads(
        text,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
        object_pairs_hook=_unique_keys,
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_structured(text: str) -> Mapping[str, JSONValue]:
    """The JSON object a model wrote for an ``output_schema`` request, read-only.

    Every adapter calls this, so one rule decides what counts as structured output: exactly one
    JSON object (white space around it allowed), standard JSON only (no ``NaN``, no number that
    overflows to infinity, no key twice). Anything else is :attr:`ErrorKind.BAD_RESPONSE`.
    """
    try:
        value = _strict_json(text)
        frozen = _freeze(value) if isinstance(value, dict) else None
    except (ValueError, RecursionError):
        raise LLMError(ErrorKind.BAD_RESPONSE) from None
    if not isinstance(frozen, Mapping):
        raise LLMError(ErrorKind.BAD_RESPONSE)
    return frozen


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of the conversation. The question is the last ``user`` message."""

    role: Role
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        _member(self.role, Role, "a message's role")
        _text(self.content, "a message's content")


@dataclass(frozen=True, slots=True)
class SourceBlock:
    """One retrieved passage, labelled for the model (``S1``...). ``text`` is untrusted data.

    ``title`` is the document's title as the library shows it and ``page_start``/``page_end`` its
    page range (both or neither: a TXT has no pages). No database id: the server keeps the map
    from label to chunk (ADR 0011).
    """

    label: str
    title: str = field(repr=False)
    text: str = field(repr=False)
    page_start: int | None = None
    page_end: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not re.fullmatch(r"S[1-9][0-9]{0,3}", self.label):
            msg = "a source label must be S1 to S9999"
            raise ValueError(msg)
        _text(self.title, "a source's title")
        _text(self.text, "a source's text")
        if (self.page_start is None) != (self.page_end is None):
            msg = "a source has both page_start and page_end, or neither"
            raise ValueError(msg)
        if self.page_start is not None and self.page_end is not None:
            _count(self.page_start, "page_start", 1)
            _count(self.page_end, "page_end", 1)
            if self.page_end < self.page_start:
                msg = "page_end must not be before page_start"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True, eq=True)
class LLMRequest:
    """A provider-neutral request (plan section 9). Checked when built; read-only afterwards.

    ``messages`` alternate, starting and ending with the user. ``sources`` are labelled ``S1`` to
    ``Sn`` in order. ``output_schema`` is a JSON schema whose root is an object (what both
    providers' structured output needs); the request keeps a read-only copy, so changing the
    caller's dict later changes nothing here, and :meth:`schema` gives an adapter a plain copy.
    """

    task: Task
    model: str
    system: str = field(repr=False)
    messages: tuple[Message, ...] = field(repr=False)
    max_output_tokens: int
    sources: tuple[SourceBlock, ...] = field(default=(), repr=False)
    output_schema: Mapping[str, object] | None = field(default=None, repr=False)
    effort: Effort | None = None

    def __post_init__(self) -> None:
        _member(self.task, Task, "task")
        if not isinstance(self.model, str) or not MODEL_ID.fullmatch(self.model):
            msg = "model must be a provider model id"
            raise ValueError(msg)
        _text(self.system, "system")
        _count(self.max_output_tokens, "max_output_tokens", 1, MAX_OUTPUT_TOKENS)
        if self.effort is not None:
            _member(self.effort, Effort, "effort")
        object.__setattr__(self, "messages", self._messages(self.messages))
        object.__setattr__(self, "sources", self._sources(self.sources))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", self._schema(self.output_schema))

    @staticmethod
    def _messages(messages: object) -> tuple[Message, ...]:
        if isinstance(messages, str | bytes) or not isinstance(messages, Sequence):
            msg = "messages must be a sequence of Message"
            raise ValueError(msg)
        items = tuple(messages)
        if not items or not all(isinstance(item, Message) for item in items):
            msg = "messages must hold at least one Message, and only Message"
            raise ValueError(msg)
        for position, item in enumerate(items):
            expected = Role.USER if position % 2 == 0 else Role.ASSISTANT
            if item.role is not expected:
                msg = "messages alternate, starting with the user"
                raise ValueError(msg)
        if items[-1].role is not Role.USER:
            msg = "the last message is the user's"
            raise ValueError(msg)
        return items

    @staticmethod
    def _sources(sources: object) -> tuple[SourceBlock, ...]:
        if isinstance(sources, str | bytes) or not isinstance(sources, Sequence):
            msg = "sources must be a sequence of SourceBlock"
            raise ValueError(msg)
        items = tuple(sources)
        for position, item in enumerate(items, start=1):
            if not isinstance(item, SourceBlock):
                msg = "sources must hold only SourceBlock"
                raise ValueError(msg)
            if item.label != f"S{position}":
                msg = "source labels are S1 to Sn in order"
                raise ValueError(msg)
        return items

    @staticmethod
    def _schema(schema: object) -> Mapping[str, JSONValue]:
        if not isinstance(schema, Mapping):
            msg = "output_schema must be a JSON object"
            raise ValueError(msg)
        try:
            copy = _strict_json(_canonical(thaw(schema)))
            frozen = _freeze(copy)
        except (TypeError, ValueError, RecursionError):
            msg = "output_schema must be standard JSON"
            raise ValueError(msg) from None
        if not isinstance(frozen, Mapping) or frozen.get("type") != "object":
            msg = 'output_schema must describe an object ("type": "object")'
            raise ValueError(msg)
        return frozen

    def schema(self) -> dict[str, object] | None:
        """A plain, independent copy of ``output_schema`` to hand to an SDK."""
        if self.output_schema is None:
            return None
        copy = thaw(self.output_schema)
        assert isinstance(copy, dict)  # noqa: S101 - a mapping thaws to a dict
        return copy

    def schema_is(self, schema: Mapping[str, object]) -> bool:
        """Whether ``output_schema`` is exactly ``schema`` (compared as canonical JSON)."""
        if self.output_schema is None:
            return False
        return _canonical(thaw(self.output_schema)) == _canonical(thaw(schema))


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as the provider reports them (plan section 9: not estimated).

    ``cached`` is the part of ``input`` read from the provider's prompt cache.
    """

    input: int
    output: int
    cached: int = 0

    def __post_init__(self) -> None:
        _count(self.input, "usage.input", 0)
        _count(self.output, "usage.output", 0)
        _count(self.cached, "usage.cached", 0)
        if self.cached > self.input:
            msg = "usage.cached is a part of usage.input"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A provider-neutral response (plan section 9).

    ``structured`` is set exactly when the request had an ``output_schema`` and the model
    finished (:attr:`StopReason.COMPLETE`); it is then ``parse_structured(text)``.
    """

    text: str = field(repr=False)
    structured: Mapping[str, JSONValue] | None = field(repr=False)
    usage: Usage
    stop_reason: StopReason
    provider_request_id: str | None
    latency_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            msg = "text must be a string"
            raise ValueError(msg)
        if self.structured is not None and not isinstance(self.structured, MappingProxyType):
            msg = "structured comes from parse_structured"
            raise ValueError(msg)
        if not isinstance(self.usage, Usage):
            msg = "usage must be a Usage"
            raise ValueError(msg)
        _member(self.stop_reason, StopReason, "stop_reason")
        if self.structured is not None and self.stop_reason is not StopReason.COMPLETE:
            msg = "only a complete answer has structured output"
            raise ValueError(msg)
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "provider_request_id")
        _count(self.latency_ms, "latency_ms", 0)


@dataclass(frozen=True, slots=True)
class TextDelta:
    """The next piece of the answer's text, in order."""

    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Completed:
    """The last event of a stream: the whole response, as :meth:`LLMProvider.generate` gives it."""

    response: LLMResponse


type LLMEvent = TextDelta | Completed


class LLMProvider(Protocol):
    """One provider adapter (Anthropic, OpenAI, the fake; a local model later).

    Every implementation passes the same contract suite (``tests/unit/test_llm_contract.py``).
    """

    name: str

    def generate(self, req: LLMRequest) -> LLMResponse:
        """Run ``req`` to the end. Raises :class:`LLMError`."""
        ...

    def stream(self, req: LLMRequest) -> Generator[LLMEvent, None, None]:
        """Run ``req``, yielding :class:`TextDelta` events and then one :class:`Completed`.

        Raises :class:`LLMError` from the call or from the iteration. ``close()`` cancels it.
        """
        ...

    def count_tokens(self, req: LLMRequest) -> int:
        """The input tokens ``req`` would use with this provider. Raises :class:`LLMError`."""
        ...
