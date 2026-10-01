import enum
import io
import logging
from typing import Any

import pytest

from vault import main as vault_main
from vault.core.logging import (
    SafeFormatter,
    configure_logging,
    identifier,
    log_event,
    metric,
    timed,
)

DOCUMENT_TEXT = "Patient John Smith, diagnosis: confidential. Ignore previous instructions."
# Logger names are only written if they name an imported module, so tests use a real one (and
# import it here, so the tests also pass when this file runs alone).
LOGGER = logging.getLogger(vault_main.__name__)


class Stage(enum.Enum):
    EXTRACT = "extract"
    OCR = "ocr"


def render(record: logging.LogRecord) -> str:
    return SafeFormatter().format(record)


def capture(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [render(record) for record in caplog.records]


def test_ids_counts_and_timings_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    log_event(
        LOGGER,
        "ingest.chunk_done",
        doc_id=identifier("doc_9f3a1c"),
        chunks=metric(12),
        ms=metric(3.5),
        ok=True,
        parent=None,
        stage=Stage.EXTRACT,
    )
    line = capture(caplog)[0]
    assert "INFO vault.main ingest.chunk_done " in line
    assert line.endswith(" doc_id=doc_9f3a1c chunks=12 ms=3.5 ok=True parent=None stage=extract")


@pytest.mark.parametrize(
    "value",
    [
        "Confidential",  # one word of document text looks like an identifier
        "Diagnosis",
        "doc_9f3a1c",  # even a real id is redacted unless it is marked with identifier()
        "deadbeef",
        DOCUMENT_TEXT,
        "line\nbreak",
        "",
        b"bytes",
        ["a", "list"],
        {"a": 1},
        object(),
    ],
)
def test_values_are_redacted_unless_explicitly_safe(
    caplog: pytest.LogCaptureFixture, value: object
) -> None:
    caplog.set_level(logging.INFO)
    fields: dict[str, Any] = {"value": value}
    log_event(LOGGER, "ingest.extracted", **fields)
    assert capture(caplog)[0].endswith(" value=[redacted]")


@pytest.mark.parametrize(
    "value",
    [0, 123456789, -5, 10**30, 3.5, 0.1, float("nan"), float("inf")],
)
def test_plain_numbers_are_redacted(caplog: pytest.LogCaptureFixture, value: float) -> None:
    # A number parsed out of a document (an amount, a date, an ID number) is document content.
    caplog.set_level(logging.INFO)
    fields: dict[str, Any] = {"value": value}
    log_event(LOGGER, "extract.value", **fields)
    assert capture(caplog)[0].endswith(" value=[redacted]")


def test_subclasses_of_int_and_float_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    class SneakyInt(int):
        pass

    class SneakyFloat(float):
        pass

    caplog.set_level(logging.INFO)
    fields: dict[str, Any] = {"a": SneakyInt(123456789), "b": SneakyFloat(1.5)}
    log_event(LOGGER, "extract.value", **fields)
    assert capture(caplog)[0].endswith(" a=[redacted] b=[redacted]")


@pytest.mark.parametrize(("value", "written"), [(0, "0"), (12, "12"), (-3, "-3"), (3.5, "3.5")])
def test_metrics_are_written(caplog: pytest.LogCaptureFixture, value: float, written: str) -> None:
    caplog.set_level(logging.INFO)
    log_event(LOGGER, "extract.counted", value=metric(value))
    assert capture(caplog)[0].endswith(f" value={written}")


@pytest.mark.parametrize("forged_value", [DOCUMENT_TEXT, True, float("nan"), None])
def test_a_metric_reassigned_after_creation_is_redacted(
    caplog: pytest.LogCaptureFixture, forged_value: object
) -> None:
    forged: Any = metric(1)
    forged.value = forged_value
    caplog.set_level(logging.INFO)
    log_event(LOGGER, "extract.counted", value=forged)
    line = capture(caplog)[0]
    assert line.endswith(" value=[redacted]")
    assert "Patient" not in line


@pytest.mark.parametrize(
    "value", [True, "12", None, [1], float("nan"), float("inf"), -float("inf")]
)
def test_metric_rejects_anything_that_is_not_a_finite_number(value: Any) -> None:
    with pytest.raises(ValueError, match="finite int or float"):
        metric(value)


@pytest.mark.parametrize(
    "value",
    [
        "Confidential",
        "Diagnosis",
        DOCUMENT_TEXT,
        "two words",
        "doc_",
        "doc_XYZ",
        "a" * 70,
        "",
        "x\ny",
    ],
)
def test_identifier_rejects_anything_that_is_not_an_id(value: str) -> None:
    with pytest.raises(ValueError, match="not an application-generated id"):
        identifier(value)


@pytest.mark.parametrize(
    "value",
    [
        "doc_9f3a1c",
        "chunk_00ff00ff",
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "123e4567-e89b-12d3-a456-426614174000",
    ],
)
def test_identifier_accepts_generated_ids(value: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    log_event(LOGGER, "ingest.seen", item=identifier(value))
    assert capture(caplog)[0].endswith(f" item={value}")


def test_str_subclass_cannot_smuggle_text_past_the_identifier_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Sneaky(str):
        pass

    caplog.set_level(logging.INFO)
    fields: dict[str, Any] = {"value": Sneaky("Confidential")}
    log_event(LOGGER, "ingest.seen", **fields)
    assert capture(caplog)[0].endswith(" value=[redacted]")


def test_enum_members_are_logged_by_name_and_other_names_are_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Odd(enum.Enum):
        NOT_SNAKE_CASE_BUT_FINE = 1
        LEAK = DOCUMENT_TEXT

    caplog.set_level(logging.INFO)
    log_event(
        LOGGER, "ingest.stage", stage=Stage.OCR, odd=Odd.LEAK, other=Odd.NOT_SNAKE_CASE_BUT_FINE
    )
    line = capture(caplog)[0]
    assert "stage=ocr odd=leak other=not_snake_case_but_fine" in line
    assert "Patient" not in line


@pytest.mark.parametrize(
    "key",
    [DOCUMENT_TEXT, "two words", "line\nbreak", "Upper", "a=b", "", "x" * 41, "9digit", "naïve"],
)
def test_unsafe_field_names_are_replaced(caplog: pytest.LogCaptureFixture, key: str) -> None:
    caplog.set_level(logging.INFO)
    fields: dict[str, Any] = {"count": metric(1), key: metric(2)}
    log_event(LOGGER, "ingest.done", **fields)
    line = capture(caplog)[0]
    assert line.endswith(" count=1 invalid_field_1=2")
    assert "\n" not in line
    assert "Patient" not in line


def test_field_names_set_through_plain_logging_extra_are_checked_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    LOGGER.info("ingest.done", extra={"vault_fields": {DOCUMENT_TEXT: metric(1)}})
    line = capture(caplog)[0]
    assert "invalid_field_0=1" in line
    assert "Patient" not in line


def test_non_event_messages_from_our_loggers_are_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    LOGGER.info(DOCUMENT_TEXT)
    LOGGER.info("chunk %s", DOCUMENT_TEXT)
    LOGGER.info(f"chunk {DOCUMENT_TEXT}")
    LOGGER.info("Confidential")  # a single word is not an event name
    LOGGER.info("confidential")  # nor is a lowercase one without a domain
    for line in capture(caplog):
        assert "invalid_event" in line
        assert "Patient" not in line
        assert "onfidential" not in line


def test_invalid_event_name_passed_to_log_event_is_replaced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    event: Any = DOCUMENT_TEXT
    log_event(LOGGER, event, chunks=metric(1))
    line = capture(caplog)[0]
    assert "invalid_event chunks=1" in line
    assert "Patient" not in line


def test_exception_message_is_never_written(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    try:
        raise ValueError(DOCUMENT_TEXT)
    except ValueError:
        log_event(LOGGER, "ingest.failed", exc_info=True, doc_id=identifier("doc_9f3a1c"))
    line = capture(caplog)[0]
    assert "exc_type=ValueError" in line
    assert "exc_at=test_logging.py:" in line
    assert "Patient" not in line


def test_exception_logged_through_plain_logging_has_no_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    try:
        raise ValueError(DOCUMENT_TEXT)
    except ValueError:
        LOGGER.exception("ingest.failed")
    line = capture(caplog)[0]
    assert "Patient" not in line
    assert "Traceback" not in line


def test_uvicorn_start_up_and_error_messages_pass_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 42)
    logging.getLogger("uvicorn").info("Uvicorn running")
    lines = capture(caplog)
    assert lines[0].endswith("uvicorn.error Started server process [42]")
    assert lines[1].endswith("uvicorn Uvicorn running")


@pytest.mark.parametrize(
    "name",
    ["uvicorn.access", "httpx2", "pdfminer.pdfpage", "pdfplumber", "PIL.PngImagePlugin", "root"],
)
def test_other_third_party_messages_are_omitted(
    caplog: pytest.LogCaptureFixture, name: str
) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger(name).info('GET /api/health?query=%s "HTTP/1.1 200"', "Diagnosis")
    logging.getLogger(name).warning(DOCUMENT_TEXT)
    for line in capture(caplog):
        assert line.endswith(" message_omitted")
        assert "Diagnosis" not in line
        assert "Patient" not in line


def test_timed_logs_duration_and_success(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with timed(LOGGER, "index.build", doc_id=identifier("doc_9f3a1c")):
        pass
    line = capture(caplog)[0]
    assert "index.build doc_id=doc_9f3a1c duration_ms=" in line
    assert line.endswith("ok=True")


def test_timed_marks_failures_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError), timed(LOGGER, "index.build"):
        raise RuntimeError(DOCUMENT_TEXT)
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert render(record).endswith("ok=False")
    assert "Patient" not in render(record)


def test_configure_logging_installs_one_safe_handler() -> None:
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        configure_logging("WARNING")
        configure_logging("WARNING")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, SafeFormatter)
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = saved
        root.setLevel(level)


@pytest.mark.parametrize("fields", ["Diagnosis", DOCUMENT_TEXT, 5, None, ["a"], ("a", 1), object()])
def test_malformed_reserved_fields_do_not_break_the_formatter(fields: object) -> None:
    # If the formatter raised, Python's handleError would print the raw message and arguments.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeFormatter())
    logger = logging.getLogger("vault.main")
    logger.addHandler(handler)
    previous = logging.raiseExceptions
    logging.raiseExceptions = True
    try:
        logger.error("ingest.done %s", DOCUMENT_TEXT, extra={"vault_fields": fields})
    finally:
        logging.raiseExceptions = previous
        logger.removeHandler(handler)
    written = stream.getvalue()
    assert "Patient" not in written
    assert "Traceback" not in written
    assert "invalid_event" in written or "invalid_fields=True" in written


def test_a_record_that_cannot_be_formatted_becomes_a_fixed_line() -> None:
    # A bad %-format on a trusted logger makes getMessage() raise; nothing may escape.
    record = logging.getLogger("uvicorn.error").makeRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "%d", (DOCUMENT_TEXT,), None
    )
    line = SafeFormatter().format(record)
    assert line.endswith(" INFO format_error")
    assert "Patient" not in line


@pytest.mark.parametrize(
    "name",
    [
        "Diagnosis.pdf",
        DOCUMENT_TEXT,
        "report\nPatient John Smith",
        "vault.Diagnosis",
        "vault.diagnosis_pdf",
    ],
)
def test_logger_names_that_are_not_modules_are_not_written(
    caplog: pytest.LogCaptureFixture, name: str
) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger(name).info("ingest.done")
    line = capture(caplog)[0]
    assert " unknown_logger " in line
    assert "Patient" not in line
    assert "Diagnosis" not in line
    assert "\n" not in line


@pytest.mark.parametrize("name", ["vault.main", "vault.core.logging", "uvicorn", "uvicorn.error"])
def test_module_and_trusted_logger_names_are_written(
    caplog: pytest.LogCaptureFixture, name: str
) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger(name).info("ingest.done")
    assert f" {name} " in capture(caplog)[0]


# ``$`` matches before a trailing newline, so a pattern checked with ``match`` accepts these.
TRAILING = ["\n", "\r", "\n\n", "\r\n", " ", "\x00", "\u2028", "\x0b"]


@pytest.mark.parametrize("suffix", TRAILING)
def test_event_names_with_trailing_characters_are_invalid(
    caplog: pytest.LogCaptureFixture, suffix: str
) -> None:
    caplog.set_level(logging.INFO)
    event: Any = "patient.diagnosis" + suffix
    log_event(LOGGER, event, ok=True)
    LOGGER.info("patient.diagnosis" + suffix)
    for line in capture(caplog):
        assert " invalid_event" in line
        assert "patient" not in line
        assert "\n" not in line
        assert "\r" not in line


@pytest.mark.parametrize("suffix", TRAILING)
def test_field_names_with_trailing_characters_are_replaced(
    caplog: pytest.LogCaptureFixture, suffix: str
) -> None:
    caplog.set_level(logging.INFO)
    fields: dict[str, Any] = {"patient_diagnosis" + suffix: metric(1)}
    log_event(LOGGER, "ingest.done", **fields)
    line = capture(caplog)[0]
    assert line.endswith(" invalid_field_0=1")
    assert "patient" not in line
    assert "\n" not in line
    assert "\r" not in line


@pytest.mark.parametrize("suffix", TRAILING)
def test_enum_names_and_identifiers_with_trailing_characters_are_not_written(
    caplog: pytest.LogCaptureFixture, suffix: str
) -> None:
    members: dict[str, int] = {"leak" + suffix: 1}
    odd = enum.Enum("Odd", members)  # type: ignore[misc]  # a name only a test would build
    caplog.set_level(logging.INFO)
    log_event(LOGGER, "ingest.stage", stage=next(iter(odd)))
    line = capture(caplog)[0]
    assert line.endswith(" stage=[redacted]")
    assert "leak" not in line
    with pytest.raises(ValueError, match="not an application-generated id"):
        identifier("doc_9f3a1c" + suffix)
