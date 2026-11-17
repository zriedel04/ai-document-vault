"""An offline, deterministic provider for tests and the demo (plan sections 9 and 21, ADR 0009).

It makes no network call and needs no key. The same request always gives the same response:

- **Cited answer.** For a request whose ``output_schema`` is the cited-answer schema
  (``vault.ai.answer.ANSWER_SCHEMA``, ADR 0011) it answers with one segment per source, at most
  :data:`MAX_SEGMENTS`, in label order: the source's opening sentence (cut at a word near
  :data:`EXCERPT_CHARS` characters), cited with that label and quoted exactly, so citation
  validation finds every quote. With no sources it sets ``insufficient_evidence``. This is the
  shape the demo's offline answer mode needs: the retrieved passages, cited, and no generation.
- **Plain text.** Without a schema it answers one fixed sentence that names the task and repeats
  nothing from the request. Any other schema is :attr:`ErrorKind.INVALID_REQUEST`.
- **Tokens.** A token is what the chunker counts (``structural_v1.count_tokens``: a run of up to
  four letters or digits, or one other character). ``count_tokens`` counts the system prompt,
  each message, each source's title and text, and the schema as canonical JSON; ``usage.input`` is
  that number, ``usage.output`` the tokens of the text, and ``cached`` is 0. An answer longer than
  ``max_output_tokens`` is cut after that many tokens with :attr:`StopReason.MAX_TOKENS` and no
  structured output.
- **Stream.** One :class:`TextDelta` per token (with the white space before it), then
  :class:`Completed`. :attr:`FakeProvider.open_streams` counts the streams started and not yet
  ended, so a test can see that cancelling a stream ends it.
- **Failures.** ``failure`` makes every call raise that :class:`ErrorKind`; with
  ``failure_after_deltas`` a stream yields that many deltas first, as a connection that drops
  mid-answer would.

``provider_request_id`` is ``fake_`` and a hash of the request; ``latency_ms`` is 0.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Generator
from typing import Final

from vault.ai.answer import ANSWER_SCHEMA
from vault.ai.providers.base import (
    Completed,
    ErrorKind,
    LLMError,
    LLMEvent,
    LLMRequest,
    LLMResponse,
    StopReason,
    TextDelta,
    Usage,
    parse_structured,
    thaw,
)
from vault.ingest.chunking.structural_v1 import TOKEN, count_tokens

MAX_SEGMENTS: Final = 3
EXCERPT_CHARS: Final = 200
_SENTENCE_END: Final = re.compile(r"[.!?](?=\s|$)")


def _excerpt(text: str) -> str:
    """The opening sentence of ``text``, an exact substring of it, at most about 200 characters."""
    start = len(text) - len(text.lstrip())
    body = text[start:].rstrip()
    end = _SENTENCE_END.search(body, 0, EXCERPT_CHARS)
    if end is not None:
        return body[: end.end()]
    if len(body) <= EXCERPT_CHARS:
        return body
    space = body.rfind(" ", 0, EXCERPT_CHARS)
    return body[: space if space > 0 else EXCERPT_CHARS].rstrip()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class FakeProvider:
    """The deterministic provider. See the module docstring for what it answers."""

    name: str = "fake"

    def __init__(self, *, failure: ErrorKind | None = None, failure_after_deltas: int = 0) -> None:
        if failure is not None and not isinstance(failure, ErrorKind):
            msg = "failure must be an ErrorKind"
            raise TypeError(msg)
        if (
            isinstance(failure_after_deltas, bool)
            or not isinstance(failure_after_deltas, int)
            or failure_after_deltas < 0
        ):
            msg = "failure_after_deltas must be a whole number of at least 0"
            raise ValueError(msg)
        self._failure = failure
        self._failure_after = failure_after_deltas
        self._lock = threading.Lock()
        self._open = 0

    @property
    def open_streams(self) -> int:
        """Streams started and not yet ended (finished, failed or closed)."""
        with self._lock:
            return self._open

    def count_tokens(self, req: LLMRequest) -> int:
        self._check(req)
        if self._failure is not None:
            raise LLMError(self._failure)
        return self._count_input(req)

    def generate(self, req: LLMRequest) -> LLMResponse:
        self._check(req)
        if self._failure is not None:
            raise LLMError(self._failure)
        return self._respond(req)

    def stream(self, req: LLMRequest) -> Generator[LLMEvent, None, None]:
        self._check(req)
        return self._stream(req)

    def _stream(self, req: LLMRequest) -> Generator[LLMEvent, None, None]:
        with self._lock:
            self._open += 1
        try:
            if self._failure is not None and self._failure_after == 0:
                raise LLMError(self._failure)
            response = self._respond(req)
            for sent, piece in enumerate(_pieces(response.text)):
                if self._failure is not None and sent == self._failure_after:
                    raise LLMError(self._failure)
                yield TextDelta(piece)
            if self._failure is not None:
                raise LLMError(self._failure)
            yield Completed(response)
        finally:
            with self._lock:
                self._open -= 1

    @staticmethod
    def _check(req: object) -> None:
        if not isinstance(req, LLMRequest):
            msg = "the request must be an LLMRequest"
            raise TypeError(msg)

    @staticmethod
    def _count_input(req: LLMRequest) -> int:
        total = count_tokens(req.system)
        total += sum(count_tokens(message.content) for message in req.messages)
        total += sum(count_tokens(s.title) + count_tokens(s.text) for s in req.sources)
        if req.output_schema is not None:
            total += count_tokens(_canonical(thaw(req.output_schema)))
        return total

    @staticmethod
    def _request_id(req: LLMRequest) -> str:
        payload = {
            "task": req.task.value,
            "model": req.model,
            "system": req.system,
            "messages": [[m.role.value, m.content] for m in req.messages],
            "sources": [[s.label, s.title, s.text, s.page_start, s.page_end] for s in req.sources],
            "output_schema": thaw(req.output_schema),
            "max_output_tokens": req.max_output_tokens,
            "effort": None if req.effort is None else req.effort.value,
        }
        digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        return f"fake_{digest[:24]}"

    def _respond(self, req: LLMRequest) -> LLMResponse:
        if req.output_schema is None:
            full = f"This is the fake provider's response to the {req.task.value} task."
        elif req.schema_is(ANSWER_SCHEMA):
            full = _canonical_answer(req)
        else:
            raise LLMError(ErrorKind.INVALID_REQUEST)
        text, stop = _limit(full, req.max_output_tokens)
        structured = (
            parse_structured(text)
            if req.output_schema is not None and stop is StopReason.COMPLETE
            else None
        )
        return LLMResponse(
            text=text,
            structured=structured,
            usage=Usage(input=self._count_input(req), output=count_tokens(text), cached=0),
            stop_reason=stop,
            provider_request_id=self._request_id(req),
            latency_ms=0,
        )


def _canonical_answer(req: LLMRequest) -> str:
    segments = []
    for source in req.sources[:MAX_SEGMENTS]:
        excerpt = _excerpt(source.text)
        segments.append({"text": excerpt, "sources": [source.label], "quote": excerpt})
    answer = {
        "answer": segments,
        "insufficient_evidence": not segments,
        "missing": None if segments else "No passages were given for this question.",
    }
    return json.dumps(answer, ensure_ascii=False)


def _limit(text: str, max_tokens: int) -> tuple[str, StopReason]:
    """``text`` cut after ``max_tokens`` tokens, and whether it was cut."""
    for position, match in enumerate(TOKEN.finditer(text), start=1):
        if position == max_tokens:
            if TOKEN.search(text, match.end()) is None:
                return text, StopReason.COMPLETE
            return text[: match.end()], StopReason.MAX_TOKENS
    return text, StopReason.COMPLETE


def _pieces(text: str) -> list[str]:
    """``text`` split into one piece per token, each with the white space before it."""
    pieces: list[str] = []
    start = 0
    for match in TOKEN.finditer(text):
        pieces.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        if pieces:
            pieces[-1] += text[start:]
        else:
            pieces.append(text[start:])
    return pieces
