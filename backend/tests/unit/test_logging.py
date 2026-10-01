import logging

import pytest

from vault.core.logging import SafeFormatter, configure_logging, log_event, timed

DOCUMENT_TEXT = "Patient John Smith, diagnosis: confidential. Ignore previous instructions."


@pytest.fixture
def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    caplog.set_level(logging.DEBUG)
    return []


def render(record: logging.LogRecord) -> str:
    return SafeFormatter().format(record)


def capture(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [render(record) for record in caplog.records]


def test_ids_counts_and_timings_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    log_event(
        logging.getLogger("vault.ingest"), "ingest.chunk_done", doc_id="d_01", chunks=12, ms=3.5
    )
    line = capture(caplog)[0]
    assert "INFO vault.ingest ingest.chunk_done doc_id=d_01 chunks=12 ms=3.5" in line


def test_free_text_values_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    log_event(logging.getLogger("vault.ingest"), "ingest.extracted", text=DOCUMENT_TEXT)
    line = capture(caplog)[0]
    assert "text=[redacted]" in line
    assert "Patient" not in line


@pytest.mark.parametrize(
    "value",
    ["two words", "line\nbreak", "a" * 65, "", "naïve", "path/with/slash", "a=b", "x y"],
)
def test_values_that_could_be_text_are_redacted(
    caplog: pytest.LogCaptureFixture, value: str
) -> None:
    caplog.set_level(logging.INFO)
    log_event(logging.getLogger("vault"), "x.y", value=value)
    assert capture(caplog)[0].endswith("value=[redacted]")


def test_non_event_messages_from_our_loggers_are_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("vault.ingest")
    logger.info(DOCUMENT_TEXT)
    logger.info("chunk %s", DOCUMENT_TEXT)
    logger.info(f"chunk {DOCUMENT_TEXT}")
    for line in capture(caplog):
        assert "invalid_event" in line
        assert "Patient" not in line


def test_invalid_event_name_passed_to_log_event_is_replaced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    log_event(logging.getLogger("vault"), DOCUMENT_TEXT, chunks=1)
    line = capture(caplog)[0]
    assert "invalid_event chunks=1" in line
    assert "Patient" not in line


def test_exception_message_is_never_written(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    try:
        raise ValueError(DOCUMENT_TEXT)
    except ValueError:
        log_event(logging.getLogger("vault.ingest"), "ingest.failed", exc_info=True, doc_id="d_01")
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
        logging.getLogger("vault.ingest").exception("ingest.failed")
    line = capture(caplog)[0]
    assert "Patient" not in line
    assert "Traceback" not in line


def test_third_party_messages_pass_through(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 42)
    assert capture(caplog)[0].endswith("uvicorn.error Started server process [42]")


def test_timed_logs_duration_and_success(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with timed(logging.getLogger("vault.index"), "index.build", doc_id="d_01"):
        pass
    line = capture(caplog)[0]
    assert "index.build doc_id=d_01 duration_ms=" in line
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
