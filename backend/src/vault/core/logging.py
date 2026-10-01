"""Logging that records ids, counts and timings, and never document content.

Document text is untrusted and private (AGENTS.md hard rules), so the logging API makes it hard to
log by accident:

- Our own loggers (``vault.*``) are called through :func:`log_event`. The event name must look like
  ``ingest.chunk_done`` and every field must be a number, a boolean, ``None`` or a short
  identifier (letters, digits and ``_ . : -``). A free-text value is replaced by ``[redacted]``.
- An exception is logged as its type and the place it was raised, never its message, which could
  quote the content that failed to parse.

Records from third-party libraries (uvicorn and so on) are written as they are; they never see
document content, and extraction code that does runs behind our own loggers.
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
from typing import Final

APP_LOGGER: Final = "vault"
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
SAFE_STRING = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
REDACTED: Final = "[redacted]"
FIELDS_ATTR: Final = "vault_fields"

type FieldValue = int | float | bool | str | None


def _safe(value: object) -> str:
    """Render one field value, redacting anything that could be free text."""
    if value is None or isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, str) and SAFE_STRING.match(value):
        return value
    return REDACTED


class SafeFormatter(logging.Formatter):
    """One line per record: ``time level logger event key=value ...``."""

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        own = record.name == APP_LOGGER or record.name.startswith(f"{APP_LOGGER}.")
        message = record.getMessage() if not own else self._event(record)
        parts = [self.formatTime(record, self.datefmt), record.levelname, record.name, message]
        fields: dict[str, object] = getattr(record, FIELDS_ATTR, {})
        parts.extend(f"{key}={_safe(value)}" for key, value in fields.items())
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
    event: str,
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
def timed(logger: logging.Logger, event: str, **fields: FieldValue) -> Iterator[None]:
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
