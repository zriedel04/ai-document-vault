"""The usage ledger: the ``[pricing]`` table, the cost arithmetic, the ``ai_requests`` table and
the recorder (plan section 9, Phase 5 "usage ledger arithmetic"). Synthetic data only."""

from __future__ import annotations

import logging
import re
import sqlite3
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import Engine, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from vault.ai.providers.base import (
    PROVIDER_NAMES,
    ErrorKind,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    Role,
    SourceBlock,
    StopReason,
    Task,
    Usage,
)
from vault.ai.providers.fake import FakeProvider
from vault.ai.usage import (
    CallStatus,
    LedgerError,
    estimate_cost_nanousd,
    price_for,
    record_call,
)
from vault.core.config import ModelPrice, Settings, load_settings
from vault.db.engine import create_vault_engine
from vault.db.migrate import alembic_config, upgrade_to_latest
from vault.db.schema import AI_ERROR_KINDS, AI_REQUEST_STATUSES, AI_TASKS, ai_requests

MARKER = "ZEBRA-LEDGER-MARKER"
NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
EXAMPLE = Path(__file__).resolve().parents[3] / "config.example.toml"


@pytest.fixture(autouse=True)
def _no_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AIVAULT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


def price(inp: str, cached: str, out: str) -> ModelPrice:
    return ModelPrice(input=Decimal(inp), cached_input=Decimal(cached), output=Decimal(out))


SONNET = price("2", "0.2", "10")
PRICING = {"anthropic": {"claude-sonnet-5-5": SONNET}}


def request(model: str = "claude-sonnet-5-5", task: Task = Task.ANSWER) -> LLMRequest:
    """A request whose every text field holds MARKER."""
    return LLMRequest(
        task=task,
        model=model,
        system=f"Answer only from the sources. {MARKER}",
        messages=(Message(Role.USER, f"When is the synthetic milestone due? {MARKER}"),),
        sources=(
            SourceBlock(
                label="S1",
                title=f"Synthetic plan {MARKER}",
                text=f"The milestone is due on day 7. {MARKER}",
                page_start=1,
                page_end=1,
            ),
        ),
        max_output_tokens=500,
    )


def response(inp: int = 8000, out: int = 600, cached: int = 0, latency: int = 1234) -> LLMResponse:
    return LLMResponse(
        text=f"Day 7 [S1]. {MARKER}",
        structured=None,
        usage=Usage(input=inp, output=out, cached=cached),
        stop_reason=StopReason.COMPLETE,
        provider_request_id=f"req_{MARKER}",
        latency_ms=latency,
    )


def rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [dict(row._mapping) for row in connection.execute(select(ai_requests))]


# --- the cost arithmetic -----------------------------------------------------------------------


def test_the_plans_typical_question_costs_its_exact_price() -> None:
    # 8,000 tokens in at $2 and 600 out at $10 per million: $0.016 + $0.006.
    cost = estimate_cost_nanousd(SONNET, Usage(input=8000, output=600))
    assert cost == 22_000_000
    assert Decimal(cost).scaleb(-9) == Decimal("0.022")


def test_cached_input_is_priced_at_the_cached_price() -> None:
    # 2,000 uncached at $2, 6,000 cached at $0.20, 600 out at $10: 4000 + 1200 + 6000 micro-dollars.
    assert estimate_cost_nanousd(SONNET, Usage(input=8000, output=600, cached=6000)) == 11_200_000
    # All input cached: only the cached price applies to the input.
    assert estimate_cost_nanousd(SONNET, Usage(input=1000, output=0, cached=1000)) == 200_000


def test_prices_with_six_places_are_exact() -> None:
    # 0.1 + 0.2 is not 0.3 in binary floats; here every step is an integer.
    tiny = price("0.1", "0.2", "0.3")
    assert estimate_cost_nanousd(tiny, Usage(input=1_000_000, output=1_000_000)) == 400_000_000
    odd = price("0.000001", "0.000001", "999.999999")
    # 10^6 pico-dollars in, 999,999,999 out: 1,000,999.999 nano-dollars, rounded up.
    assert estimate_cost_nanousd(odd, Usage(input=1_000_000, output=1)) == 1_001_000


@pytest.mark.parametrize(
    ("per_mtok", "tokens", "nano"),
    [
        ("0.0005", 1, 1),  # 500 pico-dollars: exactly half, rounded up
        ("0.000499", 1, 0),  # 499 pico-dollars: rounded down
        ("0.0015", 1, 2),  # 1,500 pico-dollars: half up, not half to even
        ("0.0025", 1, 3),
        ("0.000001", 499, 0),
        ("0.000001", 500, 1),
        ("0.000001", 1500, 2),
    ],
)
def test_the_total_is_rounded_once_half_up_to_the_nano_dollar(
    per_mtok: str, tokens: int, nano: int
) -> None:
    assert estimate_cost_nanousd(price(per_mtok, "0", "0"), Usage(input=tokens, output=0)) == nano


def test_rounding_applies_to_the_sum_not_to_each_part() -> None:
    # Three parts of 400 pico-dollars each: 1,200 in all rounds to 1, not 0 + 0 + 0.
    each = price("0.0004", "0.0004", "0.0004")
    assert estimate_cost_nanousd(each, Usage(input=2, output=1, cached=1)) == 1


def test_zero_tokens_cost_exactly_zero_and_an_unknown_model_costs_unknown() -> None:
    assert estimate_cost_nanousd(SONNET, Usage(input=0, output=0)) == 0
    assert estimate_cost_nanousd(None, Usage(input=0, output=0)) is None
    assert estimate_cost_nanousd(None, Usage(input=8000, output=600)) is None


def test_a_price_is_found_for_exactly_its_provider_and_model() -> None:
    assert price_for(PRICING, "anthropic", "claude-sonnet-5-5") is SONNET
    assert price_for(PRICING, "openai", "claude-sonnet-5-5") is None
    assert price_for(PRICING, "anthropic", "claude-sonnet-5-5\n") is None
    assert price_for(PRICING, "anthropic", "CLAUDE-SONNET-5-5") is None
    assert price_for({}, "fake", "fake-1") is None


@pytest.mark.parametrize("bad", [None, (1, 2), {"input": 1}])
def test_the_arithmetic_refuses_arguments_of_the_wrong_kind(bad: object) -> None:
    with pytest.raises(ValueError, match="usage"):
        estimate_cost_nanousd(SONNET, bad)  # type: ignore[arg-type]
    if bad is not None:
        with pytest.raises(ValueError, match="price"):
            estimate_cost_nanousd(bad, Usage(input=1, output=1))  # type: ignore[arg-type]


# --- the [pricing] table -----------------------------------------------------------------------


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "c.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_prices_are_read_as_the_exact_decimals_written(tmp_path: Path) -> None:
    config = write(
        tmp_path,
        "[pricing.anthropic.claude-haiku-4-5]\ninput = 1\ncached_input = 0.1\noutput = 5.00\n"
        '[pricing.openai."gpt-5.6-luna"]\ninput = 0.2\ncached_input = 0.02\noutput = 1.2\n',
    )
    pricing = load_settings(config).pricing
    luna = pricing["openai"]["gpt-5.6-luna"]
    assert (luna.input, luna.cached_input, luna.output) == (
        Decimal("0.2"),
        Decimal("0.02"),
        Decimal("1.2"),
    )
    assert str(luna.input) == "0.2"  # the text written, not a binary float's expansion
    haiku = pricing["anthropic"]["claude-haiku-4-5"]
    assert haiku.output == Decimal(5)
    assert estimate_cost_nanousd(luna, Usage(input=3, output=0)) == 600  # 3 x $0.2 / 10^6


def test_no_pricing_table_means_no_prices() -> None:
    assert load_settings().pricing == {}
    assert Settings().pricing == {}


@pytest.mark.parametrize(
    "value",
    [
        "-1",
        "-0.000001",
        "1000.000001",
        "1e4",
        "0.0000001",  # seven places
        "1.0000000000000000000000000000001",  # more digits than a default Decimal context keeps
        "inf",
        "nan",
        "-inf",
        "true",
        '"2"',
        '"2.0"',
        "[2]",
        "{ usd = 2 }",
        "1979-05-27",
    ],
)
def test_a_price_must_be_an_exact_number_in_range(tmp_path: Path, value: str) -> None:
    config = write(
        tmp_path,
        f"[pricing.anthropic.claude-sonnet-5-5]\ninput = {value}\ncached_input = 0\noutput = 0\n",
    )
    with pytest.raises(ValidationError):
        load_settings(config)


@pytest.mark.parametrize("value", ["0", "0.0", "-0.0", "1000", "1000.000000", "0.000001", "2e2"])
def test_a_price_in_range_is_accepted(tmp_path: Path, value: str) -> None:
    config = write(
        tmp_path,
        f"[pricing.anthropic.claude-sonnet-5-5]\ninput = {value}\ncached_input = 0\noutput = 0\n",
    )
    assert load_settings(config).pricing["anthropic"]["claude-sonnet-5-5"].input == Decimal(value)


@pytest.mark.parametrize(
    "body",
    [
        # every price is required: a missing one is an error, not zero
        "[pricing.anthropic.claude-sonnet-5-5]\ninput = 2\noutput = 10\n",
        "[pricing.anthropic.claude-sonnet-5-5]\ninput = 2\ncached_input = 0.2\n",
        # no other key
        "[pricing.anthropic.claude-sonnet-5-5]\ninput = 2\ncached_input = 0.2\noutput = 10\n"
        "cache_write = 2.5\n",
        # a provider of PROVIDER_NAMES only
        "[pricing.Anthropic.claude-sonnet-5-5]\ninput = 2\ncached_input = 0.2\noutput = 10\n",
        "[pricing.google.gemini]\ninput = 2\ncached_input = 0.2\noutput = 10\n",
        # a model id as a request accepts it
        '[pricing.anthropic."claude sonnet"]\ninput = 2\ncached_input = 0.2\noutput = 10\n',
        '[pricing.anthropic."claude-sonnet-5-5\\n"]\ninput = 2\ncached_input = 0.2\noutput = 10\n',
        '[pricing.anthropic."-claude"]\ninput = 2\ncached_input = 0.2\noutput = 10\n',
        '[pricing.anthropic.""]\ninput = 2\ncached_input = 0.2\noutput = 10\n',
        # the levels are tables
        "[pricing]\nanthropic = 2\n",
        "pricing = 2\n",
        "[pricing.anthropic]\nclaude-sonnet-5-5 = 2\n",
    ],
)
def test_the_pricing_table_has_exactly_its_shape(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValidationError):
        load_settings(write(tmp_path, body))


@pytest.mark.parametrize("value", [2.0, 0.1, True, "2", None, Decimal("NaN"), Decimal("1e-7")])
def test_a_price_in_code_is_an_int_or_an_exact_decimal(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {"pricing": {"fake": {"fake-1": {"input": value, "cached_input": 0, "output": 0}}}}
        )
    assert Settings.model_validate(
        {"pricing": {"fake": {"fake-1": {"input": 2, "cached_input": Decimal("0.5"), "output": 0}}}}
    ).pricing["fake"]["fake-1"] == price("2", "0.5", "0")


def test_a_price_from_the_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prices are set in config.toml only: an environment value is text."""
    config = write(
        tmp_path,
        "[pricing.anthropic.claude-sonnet-5-5]\ninput = 2\ncached_input = 0.2\noutput = 10\n",
    )
    monkeypatch.setenv("AIVAULT_PRICING__ANTHROPIC__CLAUDE-SONNET-5-5__INPUT", "3")
    with pytest.raises(ValidationError):
        load_settings(config)


def test_other_settings_read_from_the_file_are_unchanged_by_exact_decimals(tmp_path: Path) -> None:
    """Every TOML float is now a Decimal: the whole-number settings still refuse a fraction."""
    assert load_settings(write(tmp_path, "[server]\nport = 9001\n")).server.port == 9001
    with pytest.raises(ValidationError):
        load_settings(write(tmp_path, "[server]\nport = 9001.5\n"))
    with pytest.raises(ValidationError):
        load_settings(write(tmp_path, "[workers]\nextract = 1.5\n"))


def test_the_example_config_shows_a_pricing_table_that_loads() -> None:
    """The example's [pricing] entries are commented out (prices change; an unknown price is
    recorded as unknown, a stale one as wrong). Uncommented, they must load as written."""
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = lines.index("[pricing]")
    sample: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("["):
            break
        match = re.fullmatch(r"# (\[pricing\..+\]|[a-z_]+ = [0-9.]+)", line)
        if match:
            sample.append(match.group(1))
    assert len(sample) >= 8  # the comparison is not over an empty table
    with EXAMPLE.open("rb") as handle:
        assert tomllib.load(handle)["pricing"] == {}
    settings = Settings.model_validate(
        tomllib.loads("[pricing]\n" + "\n".join(sample), parse_float=Decimal)
    )
    assert settings.pricing
    for models in settings.pricing.values():
        for model in models.values():
            assert model.cached_input <= model.input
    assert load_settings(EXAMPLE).pricing == {}


# --- the ai_requests table ---------------------------------------------------------------------


def test_the_tables_lists_are_the_applications_enums() -> None:
    assert tuple(task.value for task in Task) == AI_TASKS
    assert tuple(kind.value for kind in ErrorKind) == AI_ERROR_KINDS
    assert tuple(status.value for status in CallStatus) == AI_REQUEST_STATUSES
    assert FakeProvider.name in PROVIDER_NAMES


def test_the_table_has_no_column_for_text(engine: Engine) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("ai_requests")}
    assert columns == {
        "id",
        "created_at",
        "task",
        "provider",
        "model",
        "status",
        "error_kind",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "est_cost_nanousd",
        "latency_ms",
        "doc_count",
    }


def ok_row(**changes: Any) -> dict[str, Any]:
    return {
        "created_at": NOW.replace(tzinfo=None),
        "task": "answer",
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "status": "ok",
        "error_kind": None,
        "input_tokens": 10,
        "cached_tokens": 0,
        "output_tokens": 5,
        "est_cost_nanousd": 70_000,
        "latency_ms": 100,
        "doc_count": 1,
    } | changes


ERROR_ROW = {
    "status": "error",
    "error_kind": "timeout",
    "input_tokens": None,
    "cached_tokens": None,
    "output_tokens": None,
    "est_cost_nanousd": None,
}


@pytest.mark.parametrize(
    "row",
    [
        ok_row(),
        ok_row(est_cost_nanousd=None),
        ok_row(input_tokens=0, output_tokens=0, est_cost_nanousd=0),
        ok_row(input_tokens=10, cached_tokens=10),
        ok_row(**ERROR_ROW),
        ok_row(task="classify", provider="fake", model="fake-1"),
        ok_row(model="llama3.1:8b"),
    ],
)
def test_the_table_accepts_rows_of_the_right_shape(engine: Engine, row: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(insert(ai_requests).values(row))


@pytest.mark.parametrize(
    "row",
    [
        ok_row(task="summarise"),
        ok_row(provider="google"),
        ok_row(provider="Anthropic"),
        ok_row(model=""),
        ok_row(model="claude sonnet"),
        ok_row(model="x" * 129),
        ok_row(status="failed"),
        ok_row(error_kind="timeout"),  # ok with an error kind
        ok_row(input_tokens=None),  # ok without counts
        ok_row(cached_tokens=None),
        ok_row(output_tokens=None),
        ok_row(**(ERROR_ROW | {"error_kind": None})),
        ok_row(**(ERROR_ROW | {"error_kind": "exploded"})),
        ok_row(**(ERROR_ROW | {"input_tokens": 3})),  # an error has no counts
        ok_row(**(ERROR_ROW | {"output_tokens": 3})),
        ok_row(**(ERROR_ROW | {"est_cost_nanousd": 0})),  # or cost
        ok_row(cached_tokens=11),  # more cached than input
        ok_row(input_tokens=-1),
        ok_row(output_tokens=-1),
        ok_row(est_cost_nanousd=-1),
        ok_row(latency_ms=-1),
        ok_row(doc_count=-1),
        ok_row(input_tokens=1.5),
        ok_row(output_tokens="5x"),
        ok_row(est_cost_nanousd=0.5),
        ok_row(latency_ms="slow"),
        ok_row(doc_count=1.5),
        ok_row(latency_ms=None),
        ok_row(doc_count=None),
    ],
)
def test_the_table_refuses_rows_of_the_wrong_shape(engine: Engine, row: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(ai_requests).values(row))


@pytest.mark.parametrize("target", ["0008", "base"])
def test_the_ledger_can_be_undone_with_rows_in_it(tmp_path: Path, target: str) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        record_call(
            engine,
            PRICING,
            provider="anthropic",
            request=request(),
            outcome=response(),
            doc_count=1,
        )
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_file), target)
    connection = sqlite3.connect(db_file)
    try:
        names = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    finally:
        connection.close()
    assert "ai_requests" not in names
    assert "ix_ai_requests_created_at" not in indexes
    upgrade_to_latest(db_file)  # and done again
    engine = create_vault_engine(db_file)
    try:
        assert rows(engine) == []
    finally:
        engine.dispose()


# --- the recorder ------------------------------------------------------------------------------


def test_a_response_is_recorded_with_its_reported_usage_and_cost(engine: Engine) -> None:
    entry = record_call(
        engine,
        PRICING,
        provider="anthropic",
        request=request(),
        outcome=response(inp=8000, out=600, cached=6000, latency=987),
        doc_count=3,
        now=NOW,
    )
    assert entry.status is CallStatus.OK
    assert entry.est_cost_nanousd == 11_200_000
    assert entry.est_cost_usd == Decimal("0.0112")
    assert rows(engine) == [
        {
            "id": entry.id,
            "created_at": NOW.replace(tzinfo=None),
            "task": "answer",
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "status": "ok",
            "error_kind": None,
            "input_tokens": 8000,
            "cached_tokens": 6000,
            "output_tokens": 600,
            "est_cost_nanousd": 11_200_000,
            "latency_ms": 987,
            "doc_count": 3,
        }
    ]


def test_an_unpriced_model_is_recorded_with_an_unknown_cost(engine: Engine) -> None:
    entry = record_call(
        engine,
        PRICING,
        provider="anthropic",
        request=request(model="claude-opus-5-5"),
        outcome=response(),
        doc_count=2,
    )
    assert entry.est_cost_nanousd is None
    assert entry.est_cost_usd is None
    assert rows(engine)[0]["est_cost_nanousd"] is None
    # The same model id under another provider is not its price either.
    entry = record_call(
        engine, PRICING, provider="openai", request=request(), outcome=response(), doc_count=0
    )
    assert entry.est_cost_nanousd is None


def test_a_priced_call_with_no_tokens_costs_zero_not_unknown(engine: Engine) -> None:
    entry = record_call(
        engine,
        PRICING,
        provider="anthropic",
        request=request(),
        outcome=response(inp=0, out=0),
        doc_count=0,
    )
    assert entry.est_cost_nanousd == 0
    assert rows(engine)[0]["est_cost_nanousd"] == 0


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_an_error_is_recorded_with_its_kind_and_no_usage(engine: Engine, kind: ErrorKind) -> None:
    entry = record_call(
        engine,
        PRICING,
        provider="anthropic",
        request=request(task=Task.CLASSIFY),
        outcome=LLMError(kind),
        doc_count=1,
        latency_ms=30_000,
        now=NOW,
    )
    assert (entry.status, entry.error_kind) == (CallStatus.ERROR, kind)
    (row,) = rows(engine)
    assert row["task"] == "classify"
    assert (row["status"], row["error_kind"]) == ("error", kind.value)
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (None, None, None)
    assert row["est_cost_nanousd"] is None
    assert row["latency_ms"] == 30_000


def test_the_time_is_stored_in_utc(engine: Engine) -> None:
    later = datetime(2026, 11, 2, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    entry = record_call(
        engine,
        PRICING,
        provider="fake",
        request=request(model="fake-1"),
        outcome=response(),
        doc_count=0,
        now=later,
    )
    assert entry.created_at == NOW.replace(tzinfo=None)
    assert rows(engine)[0]["created_at"] == NOW.replace(tzinfo=None)


def test_rows_are_numbered_in_order(engine: Engine) -> None:
    ids = [
        record_call(
            engine,
            PRICING,
            provider="fake",
            request=request(model="fake-1"),
            outcome=response(),
            doc_count=0,
        ).id
        for _ in range(3)
    ]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_a_real_fake_provider_call_is_recorded(engine: Engine) -> None:
    provider = FakeProvider()
    req = request(model="fake-1")
    answer = provider.generate(req)
    entry = record_call(
        engine, {}, provider=provider.name, request=req, outcome=answer, doc_count=1
    )
    assert (entry.input_tokens, entry.output_tokens, entry.cached_tokens) == (
        answer.usage.input,
        answer.usage.output,
        answer.usage.cached,
    )
    assert entry.est_cost_nanousd is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"provider": "google"}, "provider"),
        ({"provider": "Anthropic"}, "provider"),
        ({"request": "a prompt"}, "request"),
        ({"outcome": None}, "outcome"),
        ({"outcome": RuntimeError("x")}, "outcome"),
        ({"doc_count": -1}, "doc_count"),
        ({"doc_count": True}, "doc_count"),
        ({"doc_count": 1.0}, "doc_count"),
        ({"latency_ms": 5}, "latency"),  # a response has its own
        ({"outcome": LLMError(ErrorKind.TIMEOUT)}, "latency_ms"),  # an error needs one
        ({"outcome": LLMError(ErrorKind.TIMEOUT), "latency_ms": -1}, "latency_ms"),
        ({"outcome": LLMError(ErrorKind.TIMEOUT), "latency_ms": False}, "latency_ms"),
        ({"now": datetime(2026, 11, 2, 9, 30)}, "time zone"),
        ({"outcome": response(inp=2**63)}, "larger"),
    ],
)
def test_the_recorder_refuses_arguments_of_the_wrong_kind(
    engine: Engine, changes: dict[str, Any], message: str
) -> None:
    arguments: dict[str, Any] = {
        "provider": "anthropic",
        "request": request(),
        "outcome": response(),
        "doc_count": 1,
    } | changes
    with pytest.raises(ValueError, match=message):
        record_call(engine, PRICING, **arguments)
    assert rows(engine) == []


def test_a_cost_too_large_to_store_is_refused(engine: Engine) -> None:
    dear = {"anthropic": {"claude-sonnet-5-5": price("1000", "1000", "1000")}}
    with pytest.raises(ValueError, match="larger"):
        record_call(
            engine,
            dear,
            provider="anthropic",
            request=request(),
            outcome=response(inp=2**62, out=0),
            doc_count=0,
        )
    assert rows(engine) == []


def test_nothing_from_the_request_or_the_answer_reaches_the_table_or_the_log(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    record_call(
        engine,
        PRICING,
        provider="anthropic",
        request=request(),
        outcome=response(),
        doc_count=1,
    )
    record_call(
        engine,
        PRICING,
        provider="anthropic",
        request=request(),
        outcome=LLMError(ErrorKind.BAD_RESPONSE),
        doc_count=1,
        latency_ms=5,
    )
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        assert isinstance(raw, sqlite3.Connection)
        dump = "\n".join(raw.iterdump())
    assert "INSERT INTO" in dump  # the dump holds the rows
    assert MARKER not in dump
    assert "milestone" not in dump
    assert "req_" not in dump  # nor the provider's request id
    assert [record.getMessage() for record in caplog.records] == [
        "ai.usage_recorded",
        "ai.usage_recorded",
    ]
    for record in caplog.records:
        assert MARKER not in repr(record.__dict__)
        assert "milestone" not in repr(record.__dict__)


def test_a_database_failure_is_a_ledger_error_with_fixed_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A database without the table: the error names nothing from the request or the database."""
    engine = create_vault_engine(tmp_path / "empty.db")
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(LedgerError) as raised:
            record_call(
                engine,
                PRICING,
                provider="anthropic",
                request=request(),
                outcome=response(),
                doc_count=1,
            )
    finally:
        engine.dispose()
    assert str(raised.value) == "the usage ledger could not be written"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert [record.getMessage() for record in caplog.records] == ["ai.usage_failed"]
    assert MARKER not in repr(caplog.records[0].__dict__)
