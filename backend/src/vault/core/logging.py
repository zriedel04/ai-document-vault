"""Logging that records ids, counts and timings, and never document content.

Document text is untrusted and private (AGENTS.md hard rules), so the logging API is deny by
default: a value is written only if its type says it is safe. Nothing is allowed because it merely
looks harmless (a single word of document text looks like an identifier).

- Our own loggers (``vault.*``) are called through :func:`log_event`. The event name must be a
  string literal of the form ``domain.action`` such as ``ingest.chunk_done``. The form is checked
  at run time (anything else is written as ``invalid_event``), but a string that happens to match
  it would pass, and mypy does not enforce ``LiteralString``, so never build an event name from
  data; reviewers check that every call site passes a literal.
- A field value is written only if it is a number, a boolean, ``None``, an enum member (a name
  fixed in code) or an :class:`Identifier`, which the caller creates explicitly with
  :func:`identifier` from an id the application generated (``doc_9f3a1c``, a content hash, a
  UUID). Any other value, including every plain ``str``, is replaced by ``[redacted]``.
- A field name must be ``lower_snake_case``; anything else is replaced by ``invalid_field_N``.
- An exception is logged as its type and the place it was raised, never its message, which could
  quote the content that failed to parse.

- Third-party loggers are deny by default too. Libraries log things we do not control (uvicorn's
  access log writes the request path and query, which can carry private text), so only
  ``uvicorn`` and ``uvicorn.error`` messages (start-up and errors) are written. A record from any
  other logger is written as its name and level with ``message_omitted``. The server also runs
  with ``access_log=False``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Final, LiteralString

APP_LOGGER: Final = "vault"
# Third-party loggers whose messages are written as they are. Everything else is omitted.
TRUSTED_LOGGERS: Final = frozenset({"uvicorn", "uvicorn.error"})
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")  # at least ``domain.action``
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# An application-generated id: ``doc_9f3a1c``, a content hash, or a UUID.
IDENTIFIER = re.compile(
    r"^(?:[a-z]{1,12}_[0-9a-f]{6,64}|[0-9a-f]{8,64}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})$"
)
REDACTED: Final = "[redacted]"
FIELDS_ATTR: Final = "vault_fields"


class Identifier(str):
    """An id the application generated, marked as safe to log. Create it with :func:`identifier`."""

    __slots__ = ()

    def __new__(cls, value: str) -> Identifier:
        if not IDENTIFIER.fullmatch(value):
            msg = "not an application-generated id (like doc_9f3a1c, a hex hash or a UUID)"
            raise ValueError(msg)
        return super().__new__(cls, value)


def identifier(value: str) -> Identifier:
    """Mark ``value`` as an id that is safe to log. Raises ValueError for anything else.

    Only call this on ids the application generated itself. Never pass text taken from a document
    or a file name: the check only rejects values that do not look like an id, so reviewers must
    treat every call as a place document content could enter.
    """
    return Identifier(value)


type FieldValue = int | float | bool | Identifier | Enum | None


def _safe(value: object) -> str:
    """Render one field value; anything not explicitly safe is redacted."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return str(bool(value))
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        return repr(float(value))
    if isinstance(value, Identifier) and IDENTIFIER.fullmatch(str(value)):
        return str.__str__(value)
    if isinstance(value, Enum) and FIELD_NAME.match(name := value.name.lower()):
        return name
    return REDACTED


class SafeFormatter(logging.Formatter):
    """One line per record: ``time level logger event key=value ...``."""

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        own = record.name == APP_LOGGER or record.name.startswith(f"{APP_LOGGER}.")
        if own:
            message = self._event(record)
        elif record.name in TRUSTED_LOGGERS:
            message = record.getMessage()
        else:
            message = "message_omitted"
        parts = [self.formatTime(record, self.datefmt), record.levelname, record.name, message]
        fields: dict[str, object] = getattr(record, FIELDS_ATTR, {})
        for position, (key, value) in enumerate(fields.items()):
            name = (
                key
                if isinstance(key, str) and FIELD_NAME.match(key)
                else f"invalid_field_{position}"
            )
            parts.append(f"{name}={_safe(value)}")
        if record.exc_info and (exc_type := record.exc_info[0]) is not None:
            parts.append(f"exc_type={exc_type.__name__}")
            if tb := record.exc_info[2]:
                frame = traceback.extract_tb(tb)[-1]
                parts.append(f"exc_at={os.path.basename(frame.filename)}:{frame.lineno}")
        return " ".join(parts)

    @staticmethod
    def _event(record: logging.LogRecord) -> str:
        message = record.msg
        if isinstance(message, str) and not record.args and EVENT_NAME.match(message):
            return message
        return "invalid_event"


def configure_logging(level: str = "INFO") -> None:
    """Send logs to stderr in the safe format. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def log_event(
    logger: logging.Logger,
    event: LiteralString,
    /,
    *,
    level: int = logging.INFO,
    exc_info: bool | BaseException | None = None,
    **fields: FieldValue,
) -> None:
    """Log ``event`` with ids, counts and timings as keyword fields."""
    _emit(logger, event, level, fields, exc_info)


def _emit(
    logger: logging.Logger,
    event: str,
    level: int,
    fields: dict[str, FieldValue],
    exc_info: bool | BaseException | None = None,
) -> None:
    if not EVENT_NAME.match(event):
        event = "invalid_event"
    logger.log(level, event, extra={FIELDS_ATTR: fields}, exc_info=exc_info)


@contextmanager
def timed(logger: logging.Logger, event: LiteralString, **fields: FieldValue) -> Iterator[None]:
    """Log ``event`` with ``duration_ms`` when the block ends, and ``ok=False`` if it raised."""
    started = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        _emit(
            logger,
            event,
            logging.INFO if ok else logging.WARNING,
            {**fields, "duration_ms": duration_ms, "ok": ok},
        )
