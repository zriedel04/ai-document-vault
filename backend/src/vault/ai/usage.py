"""The usage ledger: one ``ai_requests`` row per provider call, with its estimated cost (plan
section 9, "Usage tracking" and "Cost control"; Phase 5).

The AI service calls :func:`record_call` once after every ``generate`` or finished ``stream``, with
the request it sent, the provider's name, and either the :class:`LLMResponse` or the
:class:`LLMError` it got. Ask the Vault (:mod:`vault.answers.ask`) calls it for every answer call;
budgets (Phase 9), which read this table, are a later change.

**What a row holds.** The task and model from the request, the provider's name, the outcome
(``ok``, or ``error`` with its :class:`ErrorKind`), the token counts from ``LLMResponse.usage``
exactly as the provider reported them (never estimated, so an error row has none), the estimated
cost, the latency, and the number of documents the call sent passages of. Only those: the prompt,
the messages, the passages, their titles, the answer and the provider's request id are never read
from the request or the response, so none can reach the table or the log.

**The cost.** :func:`estimate_cost_nanousd` prices a call from the ``[pricing]`` table
(:class:`vault.core.config.ModelPrice`, US dollars per million tokens): the input tokens not read
from the cache at ``input``, the cached ones (``Usage.cached``, a part of ``Usage.input``) at
``cached_input``, and the output tokens at ``output``. Prices have at most six decimal places, so
the sum is exact in whole pico-dollars (10^-12 USD) with integer arithmetic, no float anywhere;
it is rounded once, half up, to the whole nano-dollar (10^-9 USD) the ledger stores. A model with
no price has an unknown cost (``None``, NULL in the table), never zero; a priced call with no
tokens costs exactly zero. Prompt-cache writes, which some providers bill above the input price,
are not in ``Usage`` and so not in the estimate.

**Errors.** Arguments of the wrong kind are a programming error (``ValueError``, fixed text). A
database failure while writing is a :class:`LedgerError` with fixed text, logged by its class name
only. The log line of a recorded call carries the row id, task, outcome and counts.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Final

from sqlalchemy import Engine, insert
from sqlalchemy.exc import SQLAlchemyError

from vault.ai.providers.base import (
    PROVIDER_NAMES,
    ErrorKind,
    LLMError,
    LLMRequest,
    LLMResponse,
    Task,
    Usage,
)
from vault.core.config import PRICE_PLACES, ModelPrice
from vault.core.logging import log_event, metric
from vault.db.engine import DatabaseError, write_transaction
from vault.db.schema import ai_requests

logger = logging.getLogger(__name__)

# The pico-dollars (10^-12 USD) in one nano-dollar (10^-9 USD), the ledger's unit.
_PICO_PER_NANO: Final = 1000
# The largest integer SQLite stores.
_SQLITE_MAX_INT: Final = 2**63 - 1


class CallStatus(Enum):
    OK = "ok"
    ERROR = "error"


class LedgerError(Exception):
    """The usage ledger could not be written. The message is fixed text."""

    def __init__(self) -> None:
        super().__init__("the usage ledger could not be written")


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One stored ``ai_requests`` row, as :func:`record_call` wrote it."""

    id: int
    created_at: datetime  # UTC, without a time zone, as every time in vault.db
    task: Task
    provider: str
    model: str
    status: CallStatus
    error_kind: ErrorKind | None
    input_tokens: int | None
    cached_tokens: int | None
    output_tokens: int | None
    est_cost_nanousd: int | None  # None: the model has no price (or the call failed)
    latency_ms: int
    doc_count: int

    @property
    def est_cost_usd(self) -> Decimal | None:
        """The estimated cost in US dollars, exactly (``Decimal``), or ``None`` when unknown."""
        if self.est_cost_nanousd is None:
            return None
        return Decimal(self.est_cost_nanousd).scaleb(-9)


def price_for(
    pricing: Mapping[str, Mapping[str, ModelPrice]], provider: str, model: str
) -> ModelPrice | None:
    """The ``[pricing]`` entry for exactly this provider and model id, or ``None``."""
    return pricing.get(provider, {}).get(model)


def _micro(price: Decimal) -> int:
    """A price as whole micro-dollars per million tokens (exact: a price has at most six places)."""
    scaled = price.scaleb(PRICE_PLACES)
    if not scaled.is_finite() or scaled != scaled.to_integral_value() or scaled < 0:
        msg = "a price must have at most six decimal places and not be negative"
        raise ValueError(msg)
    return int(scaled)


def estimate_cost_nanousd(price: ModelPrice | None, usage: Usage) -> int | None:
    """The estimated cost of ``usage`` at ``price`` in whole nano-dollars, or ``None`` (unknown)
    when there is no price. See the module docstring for the rule."""
    if not isinstance(usage, Usage):
        msg = "usage must be a Usage"
        raise ValueError(msg)
    if price is None:
        return None
    if not isinstance(price, ModelPrice):
        msg = "price must be a ModelPrice or None"
        raise ValueError(msg)
    # tokens x micro-dollars per million tokens = pico-dollars.
    pico = (
        (usage.input - usage.cached) * _micro(price.input)
        + usage.cached * _micro(price.cached_input)
        + usage.output * _micro(price.output)
    )
    return (pico + _PICO_PER_NANO // 2) // _PICO_PER_NANO


def _whole(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"{what} must be a whole number of at least 0"
        raise ValueError(msg)
    return value


def record_call(
    engine: Engine,
    pricing: Mapping[str, Mapping[str, ModelPrice]],
    *,
    provider: str,
    request: LLMRequest,
    outcome: LLMResponse | LLMError,
    doc_count: int,
    latency_ms: int | None = None,
    now: datetime | None = None,
) -> LedgerEntry:
    """Write one ``ai_requests`` row for a provider call and return it.

    ``provider`` is the adapter's ``name``; ``request`` the request sent (only its task and model
    are read); ``outcome`` the response or the error; ``doc_count`` the number of documents whose
    passages were sent. A response carries its own latency, so ``latency_ms`` is given exactly when
    ``outcome`` is an error: the time the caller measured until it failed. ``now`` is for tests.
    """
    if provider not in PROVIDER_NAMES:
        msg = "provider must be one of PROVIDER_NAMES"
        raise ValueError(msg)
    if not isinstance(request, LLMRequest):
        msg = "request must be an LLMRequest"
        raise ValueError(msg)
    _whole(doc_count, "doc_count")
    if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
        msg = "now must be a datetime with a time zone"
        raise ValueError(msg)
    created_at = (now or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)

    usage: Usage | None
    if isinstance(outcome, LLMResponse):
        if latency_ms is not None:
            msg = "a response carries its own latency: give latency_ms only for an error"
            raise ValueError(msg)
        status, error_kind, usage = CallStatus.OK, None, outcome.usage
        latency = outcome.latency_ms
        cost = estimate_cost_nanousd(price_for(pricing, provider, request.model), usage)
    elif isinstance(outcome, LLMError):
        status, error_kind, usage = CallStatus.ERROR, outcome.kind, None
        latency = _whole(latency_ms, "latency_ms")
        cost = None
    else:
        msg = "outcome must be an LLMResponse or an LLMError"
        raise ValueError(msg)

    row: dict[str, object] = {
        "created_at": created_at,
        "task": request.task.value,
        "provider": provider,
        "model": request.model,
        "status": status.value,
        "error_kind": None if error_kind is None else error_kind.value,
        "input_tokens": None if usage is None else usage.input,
        "cached_tokens": None if usage is None else usage.cached,
        "output_tokens": None if usage is None else usage.output,
        "est_cost_nanousd": cost,
        "latency_ms": latency,
        "doc_count": doc_count,
    }
    if any(isinstance(value, int) and value > _SQLITE_MAX_INT for value in row.values()):
        msg = "a usage figure is larger than the database can store"
        raise ValueError(msg)

    try:
        with write_transaction(engine) as connection:
            result = connection.execute(insert(ai_requests).values(row))
            row_id = result.inserted_primary_key[0] if result.inserted_primary_key else None
    except (SQLAlchemyError, sqlite3.Error, DatabaseError):
        log_event(logger, "ai.usage_failed", level=logging.ERROR, exc_info=True, task=request.task)
        raise LedgerError from None
    if not isinstance(row_id, int):
        raise LedgerError

    entry = LedgerEntry(
        id=row_id,
        created_at=created_at,
        task=request.task,
        provider=provider,
        model=request.model,
        status=status,
        error_kind=error_kind,
        input_tokens=None if usage is None else usage.input,
        cached_tokens=None if usage is None else usage.cached,
        output_tokens=None if usage is None else usage.output,
        est_cost_nanousd=cost,
        latency_ms=latency,
        doc_count=doc_count,
    )
    log_event(
        logger,
        "ai.usage_recorded",
        request_id=metric(row_id),
        task=request.task,
        status=status,
        error_kind=error_kind,
        input_tokens=None if usage is None else metric(usage.input),
        cached_tokens=None if usage is None else metric(usage.cached),
        output_tokens=None if usage is None else metric(usage.output),
        cost_known=cost is not None,
        latency_ms=metric(latency),
        doc_count=metric(doc_count),
    )
    return entry
