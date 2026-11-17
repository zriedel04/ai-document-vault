"""The metadata stage (``docs/architecture.md`` section 7, step 9): the properties a file states
about itself become the document's ``file_properties`` fields: a DOCX's core properties and a
PDF's document information dictionary (its ``Info``; XMP metadata is not read), each the title,
author, created and modified.

A ``metadata`` job names a document and, as its input, the SHA-256 of the bytes whose properties it
reads (the cache key the plan gives this step). The importer queues it with the ``extract`` job for
every type in ``READERS``, and ``queue_pending`` queues it at start-up for the documents imported
before the stage existed. Like the other stages it works on the document's current version only if
that version still holds those bytes; otherwise it ends without changing anything.

1. The blob's type and size are read in one snapshot, which only says what to read. The blob is
   read whole, and its length and SHA-256 are compared with the row and the job before anything
   runs on it (``BlobChangedError``), so a blob edited in place is never read as the bytes its name
   gives.
2. The reader for the type (``READERS``) runs in the sandbox (``vault.jobs.sandbox``) with the
   configured limits: the DOCX or PDF is parsed only there, never in the server. Nothing is
   locked meanwhile.
3. The answer is untrusted (a child that a hostile file took over writes its own answer), so every
   value is judged here and not in the child: names are cleaned as folder labels are (``clean``:
   NFC, control, bidirectional and invisible characters removed, white space collapsed) and cut to
   ``MAX_FIELD_CHARS``; dates must be exactly the format's own (W3CDTF for a DOCX,
   ``parse_w3cdtf``; a PDF date, ``parse_pdf_date``) and are stored in one form, the same for
   both formats (``_stored_date``). A value that is empty after cleaning, or a date that is
   malformed, out of range or ambiguous, is left out; it never fails the job, since a file's
   properties are optional and the rest of the file may be fine.
4. Under the write lock (``BEGIN IMMEDIATE``), the document's current version is read again. A
   document that no longer holds these bytes ends the job as stale. Otherwise the values replace
   the document's file properties, recorded against that version (the values come from the bytes
   alone, so they belong to whichever version holds them now), and the job is marked done in the
   same transaction (``vault.jobs.queue.Completion``). The document's state is left alone: it
   follows the extract and chunk stages.

A type with no reader (TXT, Markdown, images) has no properties: its job, which only
a hand edit or a later reader could queue, stores none and ends.

How each failure ends the attempt (ADR 0005: a failed job marks the document Failed once its
attempts are used up), by the rule ``vault.ingest.setup_failures`` holds for every stage that runs
the sandbox, the same as the extraction stage's:

- every sandbox failure that the document can cause (``DOCUMENT_FAILURES``: a timeout, the memory
  cap, a crash, a reader that raised because the file is not a readable DOCX or PDF or is a PDF
  that needs a password, an answer that cannot be read) and the checks above raise, and the
  worker records the failed attempt by the error's class name only; the queue retries it within
  the job's attempts;
- a sandbox that cannot run at all (``SETUP_FAILURES``: an entry point the allowlist refuses, a
  child that cannot be started or limited on this system) is a failure of the setup, not a result
  about the document: the job is given back without using an attempt (``JobQueue.defer``, through
  ``run_deferring_setup``) and runs again after ``SETUP_DELAY``, its error's class name showing
  why it waits; the document is never failed by it.

Nothing here logs; the worker logs the stage and the outcome. The values are the owner's own text
and stay in ``vault.db``. Errors carry fixed text only.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from sqlalchemy import Connection, Engine, exists, select

from vault.db import ids
from vault.db.engine import read_snapshot, write_transaction
from vault.db.fields import FileProperties, replace_file_properties
from vault.db.schema import MAX_FIELD_CHARS, document_versions, documents, jobs
from vault.db.schema import blobs as blob_rows
from vault.ingest.extractors import CoreProperties, PdfProperties
from vault.ingest.path_metadata import clean
from vault.ingest.setup_failures import run_deferring_setup
from vault.jobs import sandbox
from vault.jobs.queue import Completion, Job, JobQueue
from vault.jobs.sandbox import SandboxLimits
from vault.jobs.worker import Handler
from vault.storage.blobs import BlobStore

METADATA_STAGE: Final = "metadata"
# The sandbox entry point that reads each type's properties, by the blob's canonical extension
# (``vault.ingest.validate.EXTENSIONS``).
READERS: Final[Mapping[str, str]] = MappingProxyType(
    {"docx": "docx_properties", "pdf": "pdf_properties"}
)
EXTENSIONS: Final = frozenset(READERS)
# Rounds of snapshot and decision before a version that keeps appearing fails the attempt.
MAX_ROUNDS: Final = 2
# The longest date text read; the longest W3CDTF with a time is 25 characters without a fraction
# of a second, so this leaves 39 digits for one, and the longest PDF date is 23 characters.
MAX_DATE_CHARS: Final = 64
# The largest time zone offset W3CDTF's parent, XML Schema's dateTime, allows.
MAX_OFFSET: Final = timedelta(hours=14)

# W3CDTF (https://www.w3.org/TR/NOTE-datetime): a year, a month, a day, or a day with hours and
# minutes, seconds and a fraction of a second, a time always with its zone. ASCII digits only.
# Always ``fullmatch``: ``$`` would also match before a trailing newline.
_W3CDTF: Final = re.compile(
    r"(?P<year>[0-9]{4})"
    r"(?:-(?P<month>[0-9]{2})"
    r"(?:-(?P<day>[0-9]{2})"
    r"(?:T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2})(?:\.[0-9]+)?)?"
    r"(?P<zone>Z|[+-][0-9]{2}:[0-9]{2}))?)?)?"
)
# A PDF date (ISO 32000-1, 7.9.4): ``D:``, a year, then optionally the month, day, hour, minute and
# second, each only after the one before it, and a zone only after an hour (a zone on a day alone
# says nothing). The zone is ``Z`` or ``+`` / ``-`` with the hours, then optionally an apostrophe,
# the minutes and a closing apostrophe (written by PDF 1.7 producers, left out in PDF 2.0); after
# ``Z`` only a zero offset may follow (``Z00'00'``, as macOS writes). ASCII digits only, nothing
# before or after, no white space. Always ``fullmatch``.
_PDF_DATE: Final = re.compile(
    r"D:(?P<year>[0-9]{4})"
    r"(?:(?P<month>[0-9]{2})"
    r"(?:(?P<day>[0-9]{2})"
    r"(?:(?P<hour>[0-9]{2})"
    r"(?:(?P<minute>[0-9]{2})(?P<second>[0-9]{2})?)?"
    r"(?P<zone>Z(?:00(?:'(?:00'?)?)?)?"
    r"|(?P<sign>[+-])(?P<zone_hours>[0-9]{2})(?:'(?:(?P<zone_minutes>[0-9]{2})'?)?)?)?"
    r")?)?)?"
)
# XML white space, which XML Schema's date types ignore at either end of a value.
_XML_SPACE: Final = " \t\r\n"

type Runner = Callable[..., object]


class MetadataStageError(Exception):
    """The attempt could not read the version's properties. Messages are fixed text."""


class BlobChangedError(MetadataStageError):
    def __init__(self) -> None:
        super().__init__("The stored blob does not hold the bytes its hash and size name.")


class PropertiesResultError(MetadataStageError):
    def __init__(self) -> None:
        super().__init__("The reader's result is not one it can return.")


class VersionChangedError(MetadataStageError):
    def __init__(self) -> None:
        super().__init__("The document's version changed while its properties were read.")


class Outcome(StrEnum):
    STORED = "stored"  # the properties (perhaps none) were written and the job is done
    STALE = "stale"  # the document is gone or no longer holds these bytes
    # With a job only: the attempt failed and was recorded in the transaction that found it.
    RETRY = "retry"
    FAILED = "failed"


_COMPLETION: Final = {
    Outcome.STORED: Completion.RECORDED,
    Outcome.STALE: Completion.RECORDED,
    Outcome.RETRY: Completion.ATTEMPT_QUEUED,
    Outcome.FAILED: Completion.ATTEMPT_FAILED,
}


def clean_text(raw: str) -> str | None:
    """A name a file gives itself (its title, its author) as the vault stores it: cleaned as a
    folder label is (``vault.ingest.path_metadata.clean``) and cut to ``MAX_FIELD_CHARS``, or
    None when nothing is left."""
    text = clean(raw)[:MAX_FIELD_CHARS].rstrip()
    return text or None


def parse_w3cdtf(raw: str) -> str | None:
    """The date in ``raw`` as the vault stores it, or None unless ``raw`` is W3CDTF exactly.

    A year (``2026``), a month (``2026-03``) or a day (``2026-03-04``) is kept as written. A time
    must give its zone (``Z`` or an offset of at most 14 hours) and is stored in UTC to the second
    (``2026-03-04T05:06:07Z``; a fraction of a second is dropped). Refused: anything else, a month,
    day, hour, minute or second out of range (February 30, 24:00, a leap second), the year 0000,
    and a time that UTC moves outside the years 0001 to 9999. XML white space at either end is
    ignored, as XML Schema's date types do."""
    text = raw.strip(_XML_SPACE)
    if len(text) > MAX_DATE_CHARS:
        return None
    found = _W3CDTF.fullmatch(text)
    if found is None:
        return None
    if found["hour"] is None:
        return _stored_date(found["year"], found["month"], found["day"], None)
    offset = _offset(found["zone"])
    if offset is None:
        return None
    clock = (int(found["hour"]), int(found["minute"]), int(found["second"] or 0), offset)
    return _stored_date(found["year"], found["month"], found["day"], clock)


def parse_pdf_date(raw: str) -> str | None:
    """The PDF date in ``raw`` (``D:YYYYMMDDHHmmSSOHH'mm'``) as the vault stores it, or None
    unless ``raw`` is one exactly (``_PDF_DATE``), in the forms ``parse_w3cdtf`` stores.

    A year (``D:2026``), a month (``D:202603``) or a day (``D:20260304``) is kept as such
    (``2026``, ``2026-03``, ``2026-03-04``). A time is stored in UTC to the second, a missing
    minute or second being zero as the standard says, but only with its zone: the standard says a
    time without one has an unknown relation to UTC, so it is ambiguous and left out (the day
    alone is not kept either, since the zone could move it). Refused besides: a zone after a day
    alone, a zone ``Z`` with an offset other than zero, everything ``parse_w3cdtf`` refuses (a
    value out of range, the year 0000, a zone over 14 hours, a time that UTC moves outside the
    years 0001 to 9999), white space or anything else before or after, and ``D:`` left out."""
    if len(raw) > MAX_DATE_CHARS:
        return None
    found = _PDF_DATE.fullmatch(raw)
    if found is None:
        return None
    if found["hour"] is None:
        return _stored_date(found["year"], found["month"], found["day"], None)
    if found["zone"] is None:
        return None  # a local time with no stated relation to UTC
    if found["sign"] is None:
        offset: timedelta | None = timedelta(0)  # Z, perhaps with a zero offset after it
    else:
        offset = _offset(f"{found['sign']}{found['zone_hours']}:{found['zone_minutes'] or '00'}")
    if offset is None:
        return None
    clock = (
        int(found["hour"]),
        int(found["minute"] or 0),
        int(found["second"] or 0),
        offset,
    )
    return _stored_date(found["year"], found["month"], found["day"], clock)


def _stored_date(
    year_text: str,
    month_text: str | None,
    day_text: str | None,
    clock: tuple[int, int, int, timedelta] | None,
) -> str | None:
    """The one stored form of a date that a format's grammar has matched, for every format, or
    None when it is out of range: a year, a month or a day as written, or, with ``clock`` (the
    hour, minute, second and offset, given only with a day), the time in UTC to the second."""
    year = int(year_text)
    if year < 1:
        return None
    if month_text is None:
        return f"{year:04d}"
    month = int(month_text)
    if not 1 <= month <= 12:
        return None
    if day_text is None:
        return f"{year:04d}-{month:02d}"
    day = int(day_text)
    try:
        datetime(year, month, day)  # only the calendar is checked here
    except ValueError:
        return None
    if clock is None:
        return f"{year:04d}-{month:02d}-{day:02d}"
    hour, minute, second, offset = clock
    if hour > 23 or minute > 59 or second > 59:
        return None
    try:
        utc = datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset)).astimezone(
            UTC
        )
    except OverflowError:  # UTC is before the year 1 or after 9999
        return None
    # Written out, not with strftime: %Y does not pad a year below 1000 on every platform.
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}Z"
    )


def _offset(zone: str) -> timedelta | None:
    """The offset ``Z`` or ``+hh:mm`` / ``-hh:mm`` stands for, or None when out of range."""
    if zone == "Z":
        return timedelta(0)
    hours, minutes = int(zone[1:3]), int(zone[4:6])
    if minutes > 59:
        return None
    offset = timedelta(hours=hours, minutes=minutes)
    if offset > MAX_OFFSET:
        return None
    return -offset if zone[0] == "-" else offset


def properties_from(result: object) -> FileProperties:
    """The values to store from a reader's answer, each judged as the module says, or
    ``PropertiesResultError`` when the answer is not a ``CoreProperties`` or ``PdfProperties``
    of strings. Names are judged the same for both; each date by its own format's grammar."""
    parse_date: Callable[[str], str | None]
    if type(result) is CoreProperties:
        raw = (result.title, result.creator, result.created, result.modified)
        parse_date = parse_w3cdtf
    elif type(result) is PdfProperties:
        raw = (result.title, result.author, result.created, result.modified)
        parse_date = parse_pdf_date
    else:
        raise PropertiesResultError
    if not all(value is None or type(value) is str for value in raw):
        raise PropertiesResultError
    title, author, created, modified = raw
    return FileProperties(
        title=None if title is None else clean_text(title),
        author=None if author is None else clean_text(author),
        created=None if created is None else parse_date(created),
        modified=None if modified is None else parse_date(modified),
    )


def read_document_properties(
    engine: Engine,
    store: BlobStore,
    document_id: str,
    sha256: str,
    *,
    limits: SandboxLimits,
    finish: tuple[JobQueue, Job] | None = None,
    runner: Runner = sandbox.run,
) -> Outcome:
    """Read the properties of the document's current version if it holds the bytes ``sha256``.

    Raises a ``SandboxError`` subclass when the sandbox gives no result, and
    ``BlobChangedError`` or ``PropertiesResultError`` as the module says. With ``finish``, the job
    is completed in the transaction that stores the values; ``runner`` is the sandbox's ``run``
    (tests replace it)."""
    if not ids.is_ulid(document_id):  # names no document, now or later
        if finish is not None:
            queue, job = finish
            queue.complete(job.id, job.worker_id)
        return Outcome.STALE
    current = (
        select(document_versions.c.id, blob_rows.c.ext, blob_rows.c.size)
        .select_from(
            documents.join(
                document_versions,
                (document_versions.c.id == documents.c.current_version_id)
                & (document_versions.c.document_id == documents.c.id),
            ).join(blob_rows, blob_rows.c.sha256 == document_versions.c.blob_sha256)
        )
        .where(documents.c.id == document_id, document_versions.c.blob_sha256 == sha256)
    )
    for round_no in range(1, MAX_ROUNDS + 1):
        with read_snapshot(engine) as connection:
            found = connection.execute(current).first()
        properties = None
        if found is not None:
            entry = READERS.get(found.ext)
            if entry is None:
                properties = FileProperties()
            else:
                data = _read_blob(store, sha256, found.size)
                result = runner(entry, data, limits)
                del data
                properties = properties_from(result)
        with write_transaction(engine) as connection:
            now = connection.execute(current).first()
            if now is None:
                _finish(finish, connection)
                return Outcome.STALE
            if properties is None:
                # The version came back after the snapshot: read it again.
                if round_no == MAX_ROUNDS:
                    return _failed(finish, connection, VersionChangedError())
                continue
            replace_file_properties(connection, document_id, now.id, properties)
            _finish(finish, connection)
            return Outcome.STORED
    raise AssertionError  # pragma: no cover - the last round always returns


def _read_blob(store: BlobStore, sha256: str, size: int) -> bytes:
    """The blob's bytes, read whole and compared with the size its row gives and its hash."""
    with store.open(sha256) as stream:
        data = stream.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise BlobChangedError
    return data


def _finish(finish: tuple[JobQueue, Job] | None, connection: Connection) -> None:
    if finish is not None:
        queue, job = finish
        queue.complete(job.id, job.worker_id, connection=connection)


def _failed(
    finish: tuple[JobQueue, Job] | None, connection: Connection, error: MetadataStageError
) -> Outcome:
    """Record the failed attempt in the transaction that found it, or raise without a job."""
    if finish is None:
        raise error
    queue, job = finish
    status = queue.fail(job.id, job.worker_id, error, connection=connection)
    return Outcome.FAILED if status == "failed" else Outcome.RETRY


def handler(
    engine: Engine,
    queue: JobQueue,
    store: BlobStore,
    limits: SandboxLimits,
    *,
    runner: Runner = sandbox.run,
) -> Handler:
    """The worker pool's handler for ``metadata`` jobs, which ends each job in ``queue`` in the
    transaction that stores its result, or defers it when the sandbox cannot run at all."""

    def run(job: Job) -> Completion:
        def work() -> Completion:
            outcome = read_document_properties(
                engine,
                store,
                job.document_id,
                job.input_hash,
                limits=limits,
                finish=(queue, job),
                runner=runner,
            )
            return _COMPLETION[outcome]

        return run_deferring_setup(queue, job, work)

    return run


def queue_pending(engine: Engine, queue: JobQueue) -> int:
    """Queue a ``metadata`` job for every document whose current version is of a type in
    ``READERS`` and has never had one for its bytes (documents imported before this stage, or
    before their type had a reader), in one transaction. Returns how many were queued. A
    document that had one, whatever its outcome, is left alone: a failed job is the Retry
    button's to run again, so a start-up never undoes the attempts it used."""
    pending = (
        select(documents.c.id, document_versions.c.blob_sha256)
        .select_from(
            documents.join(
                document_versions,
                (document_versions.c.id == documents.c.current_version_id)
                & (document_versions.c.document_id == documents.c.id),
            ).join(blob_rows, blob_rows.c.sha256 == document_versions.c.blob_sha256)
        )
        .where(
            blob_rows.c.ext.in_(sorted(EXTENSIONS)),
            ~exists().where(
                jobs.c.document_id == documents.c.id,
                jobs.c.stage == METADATA_STAGE,
                jobs.c.input_hash == document_versions.c.blob_sha256,
            ),
        )
        .order_by(documents.c.created_at, documents.c.id)
    )
    with write_transaction(engine) as connection:
        found = connection.execute(pending).all()
        for row in found:
            queue.enqueue(row.id, METADATA_STAGE, row.blob_sha256, connection=connection)
    return len(found)
