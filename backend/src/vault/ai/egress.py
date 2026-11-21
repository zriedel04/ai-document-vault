"""The egress gate: which chunks may be sent to a cloud model (plan section 10, ADR 0010).

Before the AI service sends passages to a provider, it passes the ids of the chunks it is about to
send to :func:`check_egress`, and sends only the chunks in ``EgressDecision.allowed``. This module
is the gate only: it calls no model and opens no connection other than to ``vault.db``.

**What is withheld.** A chunk is withheld (``Withheld``) when:

- no chunk has that id (``UNKNOWN_CHUNK``), or its document row is missing (``MISSING_DOCUMENT``,
  which the foreign keys rule out but a damaged database could hold);
- its document is in the trash (``TRASHED``): only a document in one of the states of a document in
  use (``LIVE_STATES``) is sent, as search never returns a document in the trash;
- its document is Local Only (``DOCUMENT_LOCAL_ONLY``): only ``local_only = 0`` is sent;
- a label the document carries, or any label above that one, is Local Only, or is missing
  (``LABEL_LOCAL_ONLY``). An assignment counts unless the owner rejected it (a pending suggestion
  counts, as for browsing by label, ``vault.db.labels``), and a label counts for every label below
  it, as the label filter matches a label and everything under it: a Local Only collection covers
  its sub-collections. The walk up the tree uses ``UNION``, so a parent loop ends.

When a chunk has more than one reason, the first in that order is reported.

**Drop or refuse.** The plan lets the gate either refuse the call when any chunk is restricted or
drop those chunks and say so; it does not say which is the default, so the caller names one on
every call (``EgressMode``). ``DROP`` sends the rest and reports what was withheld; ``REFUSE``
sends nothing (``refused`` is true and ``allowed`` is empty) if anything was withheld.

**Fails closed.** Every check allows a value only when it is exactly the one that may be sent, so
an unexpected value withholds. A database error while reading withholds every chunk
(``DATABASE_ERROR``) and is logged by its class name only. An id that is not a positive ``int``
(``True`` included) is a programming error and raises ``ValueError``, so nothing is sent either.
Every read shares one snapshot, so the chunks, documents and labels agree with each other.

**Repeats.** A chunk id given more than once is checked and allowed once, at its first place, and
the repeats are counted (``duplicates``). ``allowed`` keeps the order of the input.

**What the gate cannot see.** It reads the database as it is when called. If the owner marks a
document Local Only after the gate has answered, the gate does not know; the caller runs it
immediately before the request is sent and sends only ``allowed``.

**Logs.** One ``ai.egress`` line with the counts and the mode, and one ``ai.egress_chunk`` line per
chunk found in the database with its id, whether it was sent, and the reason it was withheld. A
chunk id is an integer the database assigned (``AUTOINCREMENT``), never read from a document. An
id that names no chunk is only counted. No text, title, label name or path is read or logged.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

from sqlalchemy import Connection, Engine, bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from vault.core.logging import log_event, metric
from vault.db.engine import DatabaseError, read_snapshot
from vault.db.schema import LIVE_STATES

logger = logging.getLogger(__name__)

# Ids per statement: well under SQLite's oldest limit on bound parameters (999).
BATCH: Final = 500


class EgressMode(Enum):
    """What the gate does when some chunks may not be sent."""

    DROP = "drop"  # send the others and report the withheld ones
    REFUSE = "refuse"  # send nothing


class Withheld(Enum):
    """Why a chunk was not sent (the first that applies, in this order)."""

    DATABASE_ERROR = "database_error"
    UNKNOWN_CHUNK = "unknown_chunk"
    MISSING_DOCUMENT = "missing_document"
    TRASHED = "trashed"
    DOCUMENT_LOCAL_ONLY = "document_local_only"
    LABEL_LOCAL_ONLY = "label_local_only"


@dataclass(frozen=True, slots=True)
class EgressDecision:
    """The gate's answer. Send exactly ``allowed``; nothing else."""

    mode: EgressMode
    # The chunk ids that may be sent, in input order, each once. Empty when ``refused``.
    allowed: tuple[int, ...]
    # Each chunk id that is not sent because of its own check, in input order, with the reason.
    # In REFUSE mode the chunks that passed are not listed here, but are not sent either.
    withheld: tuple[tuple[int, Withheld], ...]
    # True in REFUSE mode when anything was withheld: the request must not be sent at all.
    refused: bool
    # How many entries of the input repeated an earlier one.
    duplicates: int


def check_egress(engine: Engine, chunk_ids: Sequence[int], mode: EgressMode) -> EgressDecision:
    """Decide which of ``chunk_ids`` may be sent to a cloud model. See the module docstring."""
    if not isinstance(mode, EgressMode):
        msg = "mode must be an EgressMode"
        raise ValueError(msg)
    unique: list[int] = []
    seen: set[int] = set()
    for chunk_id in chunk_ids:
        # ``type`` and not ``isinstance``: a bool is an int, and True would ask for chunk 1.
        if type(chunk_id) is not int or chunk_id < 1:
            msg = "a chunk id is a positive int"
            raise ValueError(msg)
        if chunk_id not in seen:
            seen.add(chunk_id)
            unique.append(chunk_id)
    duplicates = len(chunk_ids) - len(unique)

    found: set[int] = set()
    try:
        with read_snapshot(engine) as connection:
            reasons, found = _reasons(connection, unique)
    except (SQLAlchemyError, sqlite3.Error, DatabaseError):
        log_event(
            logger,
            "ai.egress_failed",
            level=logging.ERROR,
            exc_info=True,
            chunks=metric(len(unique)),
        )
        reasons = dict.fromkeys(unique, Withheld.DATABASE_ERROR)

    withheld = tuple((chunk_id, reasons[chunk_id]) for chunk_id in unique if chunk_id in reasons)
    passed = tuple(chunk_id for chunk_id in unique if chunk_id not in reasons)
    refused = mode is EgressMode.REFUSE and bool(withheld)
    decision = EgressDecision(
        mode=mode,
        allowed=() if refused else passed,
        withheld=withheld,
        refused=refused,
        duplicates=duplicates,
    )
    _log(decision, len(chunk_ids), found)
    return decision


def _reasons(connection: Connection, chunk_ids: list[int]) -> tuple[dict[int, Withheld], set[int]]:
    """The reason each restricted chunk is withheld, and the ids that name a chunk."""
    rows: dict[int, tuple[str | None, object, object]] = {}
    for chunk_batch in _batches(chunk_ids):
        result = connection.execute(
            text(
                "SELECT chunks.id, documents.id, documents.state,"
                " documents.local_only FROM chunks"
                " LEFT JOIN documents ON documents.id = chunks.document_id"
                " WHERE chunks.id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": chunk_batch},
        )
        for chunk_id, document_id, state, local_only in result:
            rows[int(chunk_id)] = (document_id, state, local_only)

    document_ids = sorted({row[0] for row in rows.values() if row[0] is not None})
    restricted_by_label: set[str] = set()
    for document_batch in _batches(document_ids):
        result = connection.execute(
            text(
                "WITH RECURSIVE above(document_id, label_id) AS ("
                " SELECT document_id, label_id FROM document_labels"
                " WHERE status IS NOT 'rejected' AND document_id IN :ids"
                " UNION"
                " SELECT above.document_id, labels.parent_id FROM above"
                " JOIN labels ON labels.id = above.label_id"
                " WHERE labels.parent_id IS NOT NULL"
                ")"
                " SELECT DISTINCT above.document_id FROM above"
                " LEFT JOIN labels ON labels.id = above.label_id"
                " WHERE labels.id IS NULL OR labels.local_only IS NOT 0"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": document_batch},
        )
        restricted_by_label.update(str(document_id) for (document_id,) in result)

    reasons: dict[int, Withheld] = {}
    for chunk_id in chunk_ids:
        row = rows.get(chunk_id)
        if row is None:
            reasons[chunk_id] = Withheld.UNKNOWN_CHUNK
            continue
        document_id, state, local_only = row
        if document_id is None:
            reasons[chunk_id] = Withheld.MISSING_DOCUMENT
        elif state not in LIVE_STATES:
            reasons[chunk_id] = Withheld.TRASHED
        elif type(local_only) is not int or local_only != 0:
            reasons[chunk_id] = Withheld.DOCUMENT_LOCAL_ONLY
        elif document_id in restricted_by_label:
            reasons[chunk_id] = Withheld.LABEL_LOCAL_ONLY
    return reasons, set(rows)


def _batches[T](values: Sequence[T]) -> Iterable[list[T]]:
    for start in range(0, len(values), BATCH):
        yield list(values[start : start + BATCH])


def _log(decision: EgressDecision, requested: int, found: set[int]) -> None:
    log_event(
        logger,
        "ai.egress",
        mode=decision.mode,
        requested=metric(requested),
        duplicates=metric(decision.duplicates),
        sent=metric(len(decision.allowed)),
        withheld=metric(len(decision.withheld)),
        refused=decision.refused,
    )
    sent = set(decision.allowed)
    reasons = dict(decision.withheld)
    for chunk_id in (*decision.allowed, *(chunk_id for chunk_id, _ in decision.withheld)):
        # An id that names no chunk is only counted: it is not a chunk the database assigned.
        if chunk_id not in found:
            continue
        log_event(
            logger,
            "ai.egress_chunk",
            chunk_id=metric(chunk_id),
            sent=chunk_id in sent,
            reason=reasons.get(chunk_id),
        )
