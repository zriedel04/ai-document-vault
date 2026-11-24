"""Task-based model routing (plan section 9, "Model routing (config, not code)").

A feature asks for a :class:`~vault.ai.providers.base.Task`, never a model. Which provider and
model serve each task is the ``[ai.routes.<task>]`` table of ``config.toml``::

    [ai.routes.classify]
    provider = "anthropic"
    model = "claude-haiku-4-5"
    batch = true

The rules, checked when the settings are loaded (unknown keys are an error, as everywhere):

- **Task.** A route's name is a :class:`Task` value (``answer``, ``answer_deep``, ``classify``,
  ``query_rewrite``); any other name is refused, so a typo fails at start-up.
- **Provider.** One of :data:`PROVIDER_NAMES`: the two the plan names (``anthropic``,
  ``openai``) and ``fake``, the offline provider for tests and the demo. Whether a provider can
  be *used* in this version is the registry's answer (``vault.ai.providers.registry``): only the
  fake is registered until the adapters exist, so a route to another provider loads but
  resolving its provider fails with fixed text.
- **Model.** A provider model id (``base.MODEL_ID``, the rule a request checks), so a blank id,
  white space or a trailing newline is refused. Whether the provider knows the model is not
  checked here: model ids change with the providers, and an unknown one is the provider's
  ``invalid_request``.
- **Batch.** ``batch`` is ``true`` or ``false`` (a real boolean, not text) and ``false`` when
  left out: whether the task may use the provider's batch API (the plan sets it for ``classify``).
- **Whole routes.** A route names its provider and its model; neither is filled in from the
  default, because a model id belongs to one provider.

The defaults are the plan's example routes for the three tasks that exist in the MVP's scope
(``answer``, ``answer_deep``, ``classify``). The plan marks ``query_rewrite`` "later", so it has no
default route and asking for it fails with :class:`RouteError` until the owner adds one.

:func:`route` is the resolver: a pure function from the task, the settings and the Ask screen's
per-question picker (an :class:`ModelChoice` override) to one :class:`Route`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from vault.ai.providers.base import MODEL_ID, Task

if TYPE_CHECKING:
    from vault.core.config import Settings

ProviderName = Literal["anthropic", "openai", "fake"]
PROVIDER_NAMES: Final = frozenset(get_args(ProviderName))


class ModelChoice(BaseModel):
    """A provider and one of its models: the Ask screen's picker, and the core of a route.

    Strict: the provider is exactly one of :data:`PROVIDER_NAMES` and the model is text that is a
    model id. Nothing is converted (no number becomes text, no case is folded).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: ProviderName
    model: str

    @field_validator("model")
    @classmethod
    def _model_id(cls, model: str) -> str:
        if not MODEL_ID.fullmatch(model):
            msg = "a model must be a provider model id (letters, digits and . _ : / @ -)"
            raise ValueError(msg)
        return model


class Route(ModelChoice):
    """One ``[ai.routes.<task>]`` table: a :class:`ModelChoice` and the ``batch`` flag."""

    batch: bool = False


class RouteTable(BaseModel):
    """``[ai.routes]``: one optional route per :class:`Task`, named by the task's value.

    The field names are the task values (a test ties the two). ``None`` (possible only from code,
    since TOML has no null) means the task has no route.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: Route | None = Route(provider="anthropic", model="claude-sonnet-5-5")
    answer_deep: Route | None = Route(provider="anthropic", model="claude-opus-5-5")
    classify: Route | None = Route(provider="anthropic", model="claude-haiku-4-5", batch=True)
    query_rewrite: Route | None = None

    def for_task(self, task: Task) -> Route | None:
        configured = getattr(self, task.value)
        assert configured is None or isinstance(configured, Route)  # noqa: S101 - field types
        return configured


class AISettings(BaseModel):
    """The ``[ai]`` section. Only ``routes`` for now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    routes: RouteTable = RouteTable()


class RouteError(Exception):
    """A task with no route. The message is fixed text that names only the task."""

    def __init__(self, task: Task) -> None:
        super().__init__(
            f"no model is configured for the {task.value} task: "
            f"add an [ai.routes.{task.value}] table to config.toml"
        )
        self.task = task


class InvalidChoiceError(ValueError):
    """An override or route that is not a valid provider and model. Fixed text, no value."""

    def __init__(self) -> None:
        super().__init__("the provider or model chosen is not a valid choice")


def _checked[M: ModelChoice](value: ModelChoice, kind: type[M]) -> M:
    """``value`` validated again as exactly ``kind``, so one built with ``model_construct`` or
    changed after it was built is held to the same rules as one read from ``config.toml``."""
    try:
        return kind.model_validate(dict(vars(value)))
    except ValidationError:
        raise InvalidChoiceError from None


def route(task: Task, settings: Settings, override: ModelChoice | None = None) -> Route:
    """The provider and model that serve ``task``.

    ``override`` is the Ask screen's picker for one question: when given, it wins over the
    configured route, whether or not the task has one, and the result has ``batch`` false (a
    question asked on screen waits for its answer). It must be exactly a :class:`ModelChoice`
    (not a :class:`Route`, whose ``batch`` a picker does not set). Without it, the task's
    configured route is returned, or :class:`RouteError` raised when it has none.

    Pure: reads nothing but its arguments, and creates no provider.
    """
    if not isinstance(task, Task):
        msg = "task must be a Task"
        raise TypeError(msg)
    if override is not None:
        if type(override) is not ModelChoice:
            msg = "an override must be a ModelChoice"
            raise TypeError(msg)
        choice = _checked(override, ModelChoice)
        return Route(provider=choice.provider, model=choice.model, batch=False)
    configured = settings.ai.routes.for_task(task)
    if configured is None:
        raise RouteError(task)
    return _checked(configured, Route)
