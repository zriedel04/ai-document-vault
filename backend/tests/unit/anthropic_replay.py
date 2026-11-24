"""Replays the recorded Anthropic responses through ``httpx2.MockTransport``: no network, no key.

The recordings in ``tests/fixtures/llm_recordings/anthropic/`` are synthetic, written by hand in
the API's wire format: a Server-Sent Events stream per ``/v1/messages`` answer, a JSON body per
``/v1/messages/count_tokens`` answer and an error body per error type. :class:`Replay` picks one
from what the request asks for, the way a recording proxy keyed by request would:

- ``/v1/messages`` with ``output_config.format``: ``messages.answer.cut.sse`` when
  ``max_tokens`` is at most :data:`CUT_AT`, else ``messages.answer.s<n>.sse`` for ``n``
  ``<source>`` elements in the last user turn; without a format, ``messages.text.s<n>.sse``.
- ``/v1/messages/count_tokens``: ``count_tokens.s<n>.json``.

A request no recording covers raises :class:`NoRecording`, a ``BaseException`` the SDK does not
turn into a connection error, so the test fails instead of seeing an error kind.

``failure`` makes the replay fail the way the API does: before the stream, an HTTP error with the
recorded body for that kind (a raised ``httpx2.ReadTimeout`` for a timeout, a 200 with a broken
event or body for a bad response); with ``after`` above 0, the stream sends that many text deltas
first and then an SSE ``error`` event (or the timeout, or the broken event). ``drop_after`` cuts
the connection (``httpx2.ReadError``) after that many deltas instead.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx2

from vault.ai.providers.base import ErrorKind

RECORDINGS: Final = (
    Path(__file__).resolve().parents[1] / "fixtures" / "llm_recordings" / "anthropic"
)
CUT_AT: Final = 5
SYNTHETIC_KEY: Final = "sk-ant-api03-synthetic-test-key-0000"
REQUEST_ID: Final = "req_011SynthRecordedReplay"

# The error type and HTTP status each kind is recorded with.
ERROR_RECORDS: Final = {
    ErrorKind.AUTHENTICATION: ("authentication_error", 401),
    ErrorKind.RATE_LIMITED: ("rate_limit_error", 429),
    ErrorKind.UNAVAILABLE: ("overloaded_error", 529),
    ErrorKind.INVALID_REQUEST: ("invalid_request_error", 400),
}
BROKEN_EVENT: Final = 'event: message_start\ndata: {"type": "message_start", "message": \n\n'


class NoRecording(BaseException):
    """The request asks for something no recording covers."""


def read(name: str) -> str:
    return (RECORDINGS / name).read_text(encoding="utf-8")


def events(name: str) -> list[str]:
    """The recorded stream's events, each ending with its blank line."""
    text = read(name).replace("\r\n", "\n").strip("\n")
    return [block + "\n\n" for block in text.split("\n\n")]


def error_body(error_type: str) -> bytes:
    return read(f"error.{error_type}.json").encode("utf-8")


def sse_error(error_type: str) -> str:
    body = json.loads(error_body(error_type))
    return f"event: error\ndata: {json.dumps(body)}\n\n"


class _Body(httpx2.SyncByteStream):
    """A streamed body that can fail part-way and records whether it was closed."""

    def __init__(self, chunks: list[str], failure: Exception | None, replay: Replay) -> None:
        self._chunks = chunks
        self._failure = failure
        self._replay = replay

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            yield chunk.encode("utf-8")
        if self._failure is not None:
            raise self._failure

    def close(self) -> None:
        self._replay.closed += 1


@dataclass
class Replay:
    failure: ErrorKind | None = None
    after: int = 0
    drop_after: int | None = None
    # A recording to serve for every /v1/messages request instead of the chosen one.
    override: str | None = None
    # Applied to each recorded event and each recorded JSON body, to build a variant of it.
    edit: Callable[[str], str] | None = None
    request_id: str = REQUEST_ID
    requests: list[httpx2.Request] = field(default_factory=list)
    bodies: list[dict[str, object]] = field(default_factory=list)
    opened: int = 0
    closed: int = 0

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=self.transport())

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        assert isinstance(body, dict)
        self.bodies.append(body)
        path = request.url.path
        if path == "/v1/messages/count_tokens":
            return self._count(request, body)
        if path == "/v1/messages":
            return self._messages(request, body)
        raise NoRecording

    @staticmethod
    def _sources(body: dict[str, object]) -> int:
        messages = body["messages"]
        assert isinstance(messages, list)
        content = messages[-1]["content"]
        assert isinstance(content, str)
        return content.count("<source id=")

    def _early_failure(self, request: httpx2.Request, broken: httpx2.Response) -> httpx2.Response:
        assert self.failure is not None
        if self.failure is ErrorKind.TIMEOUT:
            raise httpx2.ReadTimeout("synthetic timeout", request=request)
        if self.failure is ErrorKind.BAD_RESPONSE:
            return broken
        error_type, status = ERROR_RECORDS[self.failure]
        return httpx2.Response(
            status,
            headers={"content-type": "application/json", "request-id": REQUEST_ID},
            content=error_body(error_type),
        )

    def _count(self, request: httpx2.Request, body: dict[str, object]) -> httpx2.Response:
        if self.failure is not None:
            broken = httpx2.Response(
                200, headers={"content-type": "application/json"}, content=b'{"input_tokens": '
            )
            return self._early_failure(request, broken)
        name = f"count_tokens.s{self._sources(body)}.json"
        if not (RECORDINGS / name).is_file():
            raise NoRecording
        text = read(name) if self.edit is None else self.edit(read(name))
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", "request-id": self.request_id},
            content=text.encode("utf-8"),
        )

    def _recording(self, body: dict[str, object]) -> str:
        if self.override is not None:
            return self.override
        sources = self._sources(body)
        if "output_config" in body and "format" in body["output_config"]:  # type: ignore[operator]
            max_tokens = body["max_tokens"]
            assert isinstance(max_tokens, int)
            name = (
                "messages.answer.cut.sse"
                if max_tokens <= CUT_AT
                else (f"messages.answer.s{sources}.sse")
            )
        else:
            name = f"messages.text.s{sources}.sse"
        if not (RECORDINGS / name).is_file():
            raise NoRecording
        return name

    def _messages(self, request: httpx2.Request, body: dict[str, object]) -> httpx2.Response:
        recorded = events(self._recording(body))
        if self.edit is not None:
            recorded = [self.edit(event) for event in recorded]
        if self.failure is not None and self.after == 0:
            broken = self._stream(request, [BROKEN_EVENT], None)
            return self._early_failure(request, broken)
        limit = self.after if self.failure is not None else self.drop_after
        if limit is None:
            return self._stream(request, recorded, None)
        chunks: list[str] = []
        sent = 0
        for event in recorded:
            if sent == limit:
                break
            chunks.append(event)
            sent += '"text_delta"' in event
        if self.failure is None:
            return self._stream(request, chunks, httpx2.ReadError("synthetic drop"))
        if self.failure is ErrorKind.TIMEOUT:
            return self._stream(request, chunks, httpx2.ReadTimeout("synthetic timeout"))
        if self.failure is ErrorKind.BAD_RESPONSE:
            return self._stream(request, [*chunks, BROKEN_EVENT], None)
        error_type, _ = ERROR_RECORDS[self.failure]
        return self._stream(request, [*chunks, sse_error(error_type)], None)

    def _stream(
        self, request: httpx2.Request, chunks: list[str], failure: Exception | None
    ) -> httpx2.Response:
        self.opened += 1
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": self.request_id},
            stream=_Body(chunks, failure, self),
            request=request,
        )


@dataclass(frozen=True)
class SyntheticKey:
    """A key object like ``vault.ai.keys.ApiKey``: the text only through ``reveal``."""

    value: str = field(default=SYNTHETIC_KEY, repr=False)

    def reveal(self) -> str:
        return self.value

    def __str__(self) -> str:
        return "SyntheticKey(<hidden>)"
