import enum
import logging
from typing import Any

import pytest

from vault.core.logging import (
    SafeFormatter,
    configure_logging,
    identifier,
    log_event,
    timed,
)

DOCUMENT_TEXT = "Patient John Smith, diagnosis: confidential. Ignore previous instructions."
LOGGER = logging.getLogger("vault.ingest")


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
        chunks=12,
        ms=3.5,
        ok=True,
        parent=None,
        stage=Stage.EXTRACT,
    )
    line = capture(caplog)[0]
    assert "INFO vault.ingest ingest.chunk_done " in line
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
    fields: dict[str, Any] = {"count": 1, key: 2}
    log_event(LOGGER, "ingest.done", **fields)
    line = capture(caplog)[0]
    assert line.endswith(" count=1 invalid_field_1=2")
    assert "\n" not in line
    assert "Patient" not in line


def test_field_names_set_through_plain_logging_extra_are_checked_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    LOGGER.info("ingest.done", extra={"vault_fields": {DOCUMENT_TEXT: 1}})
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
    log_event(LOGGER, event, chunks=1)
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


def test_third_party_messages_pass_through(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 42)
    assert capture(caplog)[0].endswith("uvicorn.error Started server process [42]")


def test_timed_logs_duration_and_success(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with timed(logging.getLogger("vault.index"), "index.build", doc_id=identifier("doc_9f3a1c")):
        pass
    line = capture(caplog)[0]
    assert "index.build doc_id=doc_9f3a1c duration_ms=" in line
    assert line.endswith("ok=True")


def test_timed_marks_failures_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError), timed(logging.getLogger("vault.index"), "index.build"):
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
