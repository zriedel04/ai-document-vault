"""Task-based model routing (``vault.ai.routing``) and the provider registry (plan section 9).

Synthetic config only: every file here is written by the test.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

import vault.ai.providers.registry as registry
from vault.ai.providers.base import Task
from vault.ai.providers.fake import FakeProvider
from vault.ai.routing import (
    PROVIDER_NAMES,
    InvalidChoiceError,
    ModelChoice,
    Route,
    RouteError,
    RouteTable,
    route,
)
from vault.core.config import Settings, load_settings

REPO_ROOT = Path(__file__).resolve().parents[3]

# The plan's example routes (section 9), for the tasks that have a default.
PLAN_ROUTES = {
    Task.ANSWER: Route(provider="anthropic", model="claude-sonnet-5-5"),
    Task.ANSWER_DEEP: Route(provider="anthropic", model="claude-opus-5-5"),
    Task.CLASSIFY: Route(provider="anthropic", model="claude-haiku-4-5", batch=True),
}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith(("AIVAULT_AI", "AIVAULT_CONFIG")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # no stray config.toml


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def settings_from(tmp_path: Path, text: str) -> Settings:
    return load_settings(write(tmp_path / "c.toml", text))


# --- the route table -------------------------------------------------------------------------


def test_the_route_table_has_exactly_one_entry_per_task() -> None:
    assert set(RouteTable.model_fields) == {task.value for task in Task}


def test_the_known_providers_are_the_plans_two_and_the_fake() -> None:
    assert {"anthropic", "openai", "fake"} == PROVIDER_NAMES


def test_the_defaults_are_the_plans_example_routes() -> None:
    settings = Settings()
    for task, expected in PLAN_ROUTES.items():
        assert route(task, settings) == expected
    # The plan marks query_rewrite "later": no default route.
    assert settings.ai.routes.query_rewrite is None
    assert set(PLAN_ROUTES) | {Task.QUERY_REWRITE} == set(Task)


def test_routes_from_the_file(tmp_path: Path) -> None:
    settings = settings_from(
        tmp_path,
        '[ai.routes.answer]\nprovider = "openai"\nmodel = "gpt-5.6-terra"\n'
        '[ai.routes.query_rewrite]\nprovider = "fake"\nmodel = "fake-1"\nbatch = false\n'
        '[ai.routes.classify]\nprovider = "openai"\nmodel = "gpt-5.6-luna"\nbatch = true\n',
    )
    assert route(Task.ANSWER, settings) == Route(provider="openai", model="gpt-5.6-terra")
    assert route(Task.QUERY_REWRITE, settings) == Route(provider="fake", model="fake-1")
    assert route(Task.CLASSIFY, settings) == Route(
        provider="openai", model="gpt-5.6-luna", batch=True
    )
    # A task the file leaves out keeps its default route.
    assert route(Task.ANSWER_DEEP, settings) == PLAN_ROUTES[Task.ANSWER_DEEP]


def test_batch_is_false_when_left_out(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, '[ai.routes.classify]\nprovider = "fake"\nmodel = "m"\n')
    assert route(Task.CLASSIFY, settings).batch is False


@pytest.mark.parametrize(
    "model", ["claude-sonnet-5-5", "gpt-5.6-luna", "llama3.1:8b", "org/model@v1", "a"]
)
def test_model_ids_the_providers_use_are_accepted(tmp_path: Path, model: str) -> None:
    text = f'[ai.routes.answer]\nprovider = "fake"\nmodel = "{model}"\n'
    settings = settings_from(tmp_path, text)
    assert route(Task.ANSWER, settings).model == model


# Each a [ai.routes.answer] body (or a whole file where it starts with "[") that must not load.
INVALID_FILES = [
    # Unknown task names.
    '[ai.routes.summarize]\nprovider = "fake"\nmodel = "m"\n',
    '[ai.routes.Answer]\nprovider = "fake"\nmodel = "m"\n',
    '[ai.routes.answer-deep]\nprovider = "fake"\nmodel = "m"\n',
    # Unknown keys at each level.
    "[ai]\nroute = 1\n",
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "m"\neffort = "high"\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "m"\nbatched = true\n',
    # Unknown or misspelled providers.
    '[ai.routes.answer]\nprovider = "local"\nmodel = "m"\n',
    '[ai.routes.answer]\nprovider = "ollama"\nmodel = "m"\n',
    '[ai.routes.answer]\nprovider = "Anthropic"\nmodel = "m"\n',
    '[ai.routes.answer]\nprovider = "openai "\nmodel = "m"\n',
    '[ai.routes.answer]\nprovider = "fake\\n"\nmodel = "m"\n',
    '[ai.routes.answer]\nprovider = ""\nmodel = "m"\n',
    '[ai.routes.answer]\nprovider = 1\nmodel = "m"\n',
    # Blank or malformed model ids.
    '[ai.routes.answer]\nprovider = "fake"\nmodel = ""\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "   "\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "m\\n"\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "m\\r"\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = " m"\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "m "\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "two words"\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = "-leading-dash"\n',
    f'[ai.routes.answer]\nprovider = "fake"\nmodel = "{"m" * 129}"\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = 5\n',
    '[ai.routes.answer]\nprovider = "fake"\nmodel = ["m"]\n',
    # A route is whole: provider and model both.
    '[ai.routes.answer]\nmodel = "m"\n',
    '[ai.routes.answer]\nprovider = "fake"\n',
    "[ai.routes.answer]\nbatch = true\n",
    # batch is a real boolean.
    '[ai.routes.classify]\nprovider = "fake"\nmodel = "m"\nbatch = "true"\n',
    '[ai.routes.classify]\nprovider = "fake"\nmodel = "m"\nbatch = 1\n',
    '[ai.routes.classify]\nprovider = "fake"\nmodel = "m"\nbatch = 0\n',
    # Not a table.
    '[ai]\nroutes = "answer"\n',
    '[ai.routes]\nanswer = "fake"\n',
]


@pytest.mark.parametrize("text", INVALID_FILES)
def test_invalid_routes_are_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValidationError):
        settings_from(tmp_path, text)


@pytest.mark.parametrize(
    "value",
    [
        {"provider": "fake", "model": 5},
        {"provider": "fake", "model": b"m"},
        {"provider": "fake", "model": "m", "batch": "true"},
        {"provider": "fake", "model": "m", "batch": 1},
        {"provider": "fake", "model": "m", "batch": None},
    ],
)
def test_route_values_in_code_are_exactly_their_kind(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"ai": {"routes": {"answer": value}}})


def test_routes_are_immutable() -> None:
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.ai.routes.answer = None  # type: ignore[misc]
    answer = settings.ai.routes.answer
    assert answer is not None
    with pytest.raises(ValidationError):
        answer.model = "other"  # type: ignore[misc]


# --- environment variables -------------------------------------------------------------------


def test_the_environment_overrides_a_route_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write(
        tmp_path / "c.toml",
        '[ai.routes.classify]\nprovider = "openai"\nmodel = "gpt-5.6-luna"\nbatch = true\n',
    )
    monkeypatch.setenv("AIVAULT_AI__ROUTES__CLASSIFY__MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("AIVAULT_AI__ROUTES__CLASSIFY__BATCH", "false")
    assert route(Task.CLASSIFY, load_settings(config)) == Route(
        provider="openai", model="gpt-5.6-terra", batch=False
    )
    monkeypatch.setenv("AIVAULT_AI__ROUTES__CLASSIFY__BATCH", "true")
    assert route(Task.CLASSIFY, load_settings(config)).batch is True


def test_the_environment_can_add_a_whole_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIVAULT_AI__ROUTES__QUERY_REWRITE__PROVIDER", "fake")
    monkeypatch.setenv("AIVAULT_AI__ROUTES__QUERY_REWRITE__MODEL", "fake-1")
    assert route(Task.QUERY_REWRITE, load_settings()) == Route(provider="fake", model="fake-1")


def test_the_environment_cannot_set_half_a_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # A route the file does not name is replaced whole, so the provider is not taken from the
    # default when only the model is given.
    monkeypatch.setenv("AIVAULT_AI__ROUTES__ANSWER__MODEL", "gpt-5.6-terra")
    with pytest.raises(ValidationError):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIVAULT_AI__ROUTES__SUMMARIZE__MODEL", "m"),
        ("AIVAULT_AI__ROUTES__ANSWER__PROVIDER", "local"),
        ("AIVAULT_AI__ROUTES__ANSWER__PROVIDER", "Anthropic"),
        ("AIVAULT_AI__ROUTES__ANSWER__MODEL", ""),
        ("AIVAULT_AI__ROUTES__ANSWER__MODEL", " "),
        ("AIVAULT_AI__ROUTES__ANSWER__MODEL", "m\n"),
        ("AIVAULT_AI__ROUTES__ANSWER__EFFORT", "high"),
    ],
)
def test_the_environment_is_checked_like_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    config = write(tmp_path / "c.toml", '[ai.routes.answer]\nprovider = "fake"\nmodel = "m"\n')
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        load_settings(config)


@pytest.mark.parametrize("text", ["True", "FALSE", "1", "0", "yes", "on", "", " true", "true\n"])
def test_an_environment_batch_must_be_true_or_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    config = write(tmp_path / "c.toml", '[ai.routes.classify]\nprovider = "fake"\nmodel = "m"\n')
    monkeypatch.setenv("AIVAULT_AI__ROUTES__CLASSIFY__BATCH", text)
    with pytest.raises(ValidationError):
        load_settings(config)


# --- the resolver ----------------------------------------------------------------------------


def test_a_task_with_no_route_fails_with_fixed_text() -> None:
    with pytest.raises(RouteError) as caught:
        route(Task.QUERY_REWRITE, Settings())
    assert str(caught.value) == (
        "no model is configured for the query_rewrite task: "
        "add an [ai.routes.query_rewrite] table to config.toml"
    )
    assert caught.value.task is Task.QUERY_REWRITE


def test_a_route_removed_in_code_fails_too() -> None:
    settings = Settings.model_validate({"ai": {"routes": {"answer": None}}})
    with pytest.raises(RouteError, match="answer task"):
        route(Task.ANSWER, settings)


def test_the_override_wins_over_the_configured_route() -> None:
    settings = Settings()
    picked = ModelChoice(provider="openai", model="gpt-5.6-sol")
    assert route(Task.ANSWER, settings, picked) == Route(provider="openai", model="gpt-5.6-sol")
    # The configured route is unchanged for the next question.
    assert route(Task.ANSWER, settings) == PLAN_ROUTES[Task.ANSWER]


def test_the_override_is_never_a_batch() -> None:
    picked = ModelChoice(provider="fake", model="fake-1")
    assert route(Task.CLASSIFY, Settings(), picked).batch is False


def test_the_override_answers_a_task_with_no_route() -> None:
    picked = ModelChoice(provider="fake", model="fake-1")
    assert route(Task.QUERY_REWRITE, Settings(), picked) == Route(provider="fake", model="fake-1")


@pytest.mark.parametrize(
    "value",
    [
        {"provider": "local", "model": "m"},
        {"provider": "Anthropic", "model": "m"},
        {"provider": "fake", "model": ""},
        {"provider": "fake", "model": "   "},
        {"provider": "fake", "model": "m\n"},
        {"provider": "fake", "model": 5},
        {"provider": "fake"},
        {"model": "m"},
        {"provider": "fake", "model": "m", "batch": True},  # a picker sets no batch
        {"provider": "fake", "model": "m", "extra": 1},
    ],
)
def test_an_override_is_checked_like_a_route(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelChoice.model_validate(value)


@pytest.mark.parametrize(
    "value",
    [
        {"provider": "local", "model": "m"},
        {"provider": "fake", "model": " private model name "},
        {"provider": "fake", "model": 5},
        {"provider": "fake"},
    ],
)
def test_an_override_that_skipped_validation_is_refused_with_fixed_text(
    value: dict[str, object],
) -> None:
    sneaked = ModelChoice.model_construct(**value)  # type: ignore[arg-type]
    with pytest.raises(InvalidChoiceError) as caught:
        route(Task.ANSWER, Settings(), sneaked)
    assert str(caught.value) == "the provider or model chosen is not a valid choice"
    assert "private" not in str(caught.value)


def test_a_configured_route_that_skipped_validation_is_refused() -> None:
    broken = Route.model_construct(provider="fake", model="", batch=False)
    settings = Settings().model_copy(
        update={"ai": Settings().ai.model_copy(update={"routes": RouteTable(answer=broken)})}
    )
    with pytest.raises(InvalidChoiceError):
        route(Task.ANSWER, settings)


@pytest.mark.parametrize(
    "override",
    [
        Route(provider="fake", model="m", batch=True),
        {"provider": "fake", "model": "m"},
        ("fake", "m"),
    ],
)
def test_an_override_must_be_exactly_a_model_choice(override: object) -> None:
    with pytest.raises(TypeError):
        route(Task.ANSWER, Settings(), override)  # type: ignore[arg-type]


@pytest.mark.parametrize("task", ["answer", None, 0])
def test_the_task_must_be_a_task(task: object) -> None:
    with pytest.raises(TypeError):
        route(task, Settings())  # type: ignore[arg-type]


def test_every_task_resolves_once_it_has_a_route(tmp_path: Path) -> None:
    body = "".join(
        f'[ai.routes.{task.value}]\nprovider = "fake"\nmodel = "fake-{task.value}"\n'
        for task in Task
    )
    settings = settings_from(tmp_path, body)
    for task in Task:
        assert route(task, settings) == Route(provider="fake", model=f"fake-{task.value}")


# --- the example config ----------------------------------------------------------------------


def test_the_example_config_routes_are_the_defaults() -> None:
    example = REPO_ROOT / "config.example.toml"
    with example.open("rb") as handle:
        written = tomllib.load(handle)["ai"]["routes"]
    assert set(written) == {task.value for task in PLAN_ROUTES}
    assert load_settings(example).ai == Settings().ai


def test_the_example_config_shows_the_plans_query_rewrite_route_commented_out() -> None:
    text = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert '# [ai.routes.query_rewrite]\n# provider = "openai"\n# model = "gpt-5.6-luna"\n' in text


# --- the registry ----------------------------------------------------------------------------


def test_the_fake_and_anthropic_are_registered() -> None:
    assert set(registry.FACTORIES) == {"fake", "anthropic"}
    assert set(registry.FACTORIES) <= PROVIDER_NAMES


def test_the_registry_creates_the_fake() -> None:
    provider = registry.create_provider("fake")
    assert isinstance(provider, FakeProvider)
    assert provider.name == "fake"
    assert registry.create_provider("fake") is not provider  # a new one each time


@pytest.mark.parametrize("name", sorted(PROVIDER_NAMES - {"fake", "anthropic"}))
def test_a_known_provider_without_an_adapter_is_unavailable(name: str) -> None:
    with pytest.raises(registry.ProviderUnavailableError) as caught:
        registry.create_provider(name)
    assert str(caught.value) == "this provider is not available in this version of the app"


@pytest.mark.parametrize("name", ["local", "Fake", "fake ", "", None, 1])
def test_an_unknown_provider_name_is_refused(name: object) -> None:
    with pytest.raises(ValueError, match="not a known provider name"):
        registry.create_provider(name)  # type: ignore[arg-type]


def test_a_factory_that_reports_another_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Impostor(FakeProvider):
        name = "openai"

    monkeypatch.setattr(registry, "FACTORIES", {"fake": Impostor})
    with pytest.raises(RuntimeError):
        registry.create_provider("fake")
