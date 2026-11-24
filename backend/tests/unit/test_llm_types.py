"""The provider-neutral types check what they hold when built (vault.ai.providers.base)."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import vault
from vault.ai.answer import ANSWER_SCHEMA, read_answer
from vault.ai.providers.base import (
    RETRYABLE,
    Effort,
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
    parse_structured,
    thaw,
)
from vault.answers.citations import StructuredAnswer

SECRET = "Patient diagnosis confidential"  # noqa: S105 - synthetic private-looking text


def _nested(depth: int) -> list[object]:
    value: list[object] = []
    for _ in range(depth):
        value = [value]
    return value


def _request(**changes: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "task": Task.ANSWER,
        "model": "claude-sonnet-5-5",
        "system": "Answer from the sources.",
        "messages": (Message(Role.USER, "What is due?"),),
        "max_output_tokens": 100,
        "sources": (SourceBlock("S1", "Syllabus", "Due Friday.", 1, 2),),
        "output_schema": ANSWER_SCHEMA,
        "effort": Effort.LOW,
    }
    fields.update(changes)
    return LLMRequest(**fields)


def test_a_valid_request_builds() -> None:
    req = _request()
    assert req.sources[0].label == "S1"
    assert req.schema_is(ANSWER_SCHEMA)


@pytest.mark.parametrize(
    "model", ["gpt-5.6-luna", "claude-opus-5-5", "llama3.1:8b", "org/model@v2"]
)
def test_model_ids_that_providers_use_are_accepted(model: str) -> None:
    assert _request(model=model).model == model


@pytest.mark.parametrize("model", ["", " ", "model\n", "-leading", "two words", "a" * 129, 5, None])
def test_a_model_that_is_not_an_id_is_refused(model: object) -> None:
    with pytest.raises(ValueError, match="model"):
        _request(model=model)


@pytest.mark.parametrize("system", ["", "  \n\t", 3])
def test_a_blank_system_prompt_is_refused(system: object) -> None:
    with pytest.raises(ValueError, match="system"):
        _request(system=system)


@pytest.mark.parametrize("value", [0, -1, 128_001, True, 1.5, "100"])
def test_max_output_tokens_is_a_bounded_whole_number(value: object) -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        _request(max_output_tokens=value)


@pytest.mark.parametrize(
    ("field", "value"), [("task", "answer"), ("effort", "low"), ("task", None)]
)
def test_enum_fields_take_only_their_enum(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _request(**{field: value})


def test_effort_may_be_left_to_the_provider() -> None:
    assert _request(effort=None).effort is None


@pytest.mark.parametrize(
    "messages",
    [
        (),
        "What is due?",
        [Message(Role.ASSISTANT, "Hello")],
        [Message(Role.USER, "a"), Message(Role.USER, "b")],
        [Message(Role.USER, "a"), Message(Role.ASSISTANT, "b")],
        [("user", "a")],
        None,
    ],
)
def test_messages_alternate_from_and_to_the_user(messages: object) -> None:
    with pytest.raises(ValueError, match="message"):
        _request(messages=messages)


def test_a_conversation_with_earlier_turns_is_accepted() -> None:
    turns = [
        Message(Role.USER, "a"),
        Message(Role.ASSISTANT, "b"),
        Message(Role.USER, "c"),
    ]
    assert len(_request(messages=turns).messages) == 3


@pytest.mark.parametrize("content", ["", "   ", None])
def test_a_blank_message_is_refused(content: object) -> None:
    with pytest.raises(ValueError, match="content"):
        Message(Role.USER, content)  # type: ignore[arg-type]


def test_a_role_must_be_a_role() -> None:
    with pytest.raises(ValueError, match="role"):
        Message("user", "a")  # type: ignore[arg-type]


@pytest.mark.parametrize("labels", [["S2"], ["S1", "S1"], ["S1", "S3"], ["S2", "S1"]])
def test_source_labels_are_s1_to_sn_in_order(labels: list[str]) -> None:
    sources = [SourceBlock(label, "Doc", "Text.") for label in labels]
    with pytest.raises(ValueError, match="S1 to Sn"):
        _request(sources=sources)


def test_sources_hold_only_source_blocks() -> None:
    with pytest.raises(ValueError, match="SourceBlock"):
        _request(sources=[{"label": "S1"}])
    with pytest.raises(ValueError, match="SourceBlock"):
        _request(sources="S1")


@pytest.mark.parametrize("label", ["S0", "s1", "S01", "S1\n", "S", "S10000", "1", 1])
def test_a_label_that_is_not_s_and_a_number_is_refused(label: object) -> None:
    with pytest.raises(ValueError, match="label"):
        SourceBlock(label, "Doc", "Text.")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("start", "end"), [(1, None), (None, 1), (0, 1), (3, 2), (True, 1), (1, 2.0)]
)
def test_a_page_range_is_whole_and_ordered(start: object, end: object) -> None:
    with pytest.raises(ValueError, match="page"):
        SourceBlock("S1", "Doc", "Text.", start, end)  # type: ignore[arg-type]


@pytest.mark.parametrize(("title", "text"), [("", "Text."), ("Doc", " \n"), ("Doc", None)])
def test_a_source_needs_a_title_and_text(title: object, text: object) -> None:
    with pytest.raises(ValueError, match="source"):
        SourceBlock("S1", title, text)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array"},
        {"properties": {}},
        {"type": "object", "x": float("nan")},
        {"type": "object", "x": object()},
        ["type", "object"],
        {"type": "object", "deep": _nested(5000)},
        "object",
    ],
)
def test_an_output_schema_is_a_standard_json_object_schema(schema: object) -> None:
    with pytest.raises(ValueError, match="output_schema"):
        _request(output_schema=schema)


def test_the_request_keeps_its_own_read_only_copy_of_the_schema() -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}
    req = _request(output_schema=schema)
    schema["properties"]["a"]["type"] = "number"
    assert req.schema() == {"type": "object", "properties": {"a": {"type": "string"}}}
    assert req.output_schema is not None
    with pytest.raises(TypeError):
        req.output_schema["type"] = "array"  # type: ignore[index]
    handed_out = req.schema()
    assert handed_out is not None
    handed_out["type"] = "array"
    assert req.schema_is({"type": "object", "properties": {"a": {"type": "string"}}})


def test_a_request_cannot_be_changed_after_it_is_built() -> None:
    req = _request()
    with pytest.raises(AttributeError):
        req.model = "other"  # type: ignore[misc]


def test_a_request_without_schema() -> None:
    req = _request(output_schema=None)
    assert req.schema() is None
    assert not req.schema_is(ANSWER_SCHEMA)


def test_errors_and_reprs_never_repeat_text() -> None:
    with pytest.raises(ValueError, match="model") as raised:
        _request(model=SECRET)
    assert SECRET not in str(raised.value)
    req = _request(
        system=SECRET,
        messages=(Message(Role.USER, SECRET),),
        sources=(SourceBlock("S1", SECRET, SECRET),),
    )
    assert SECRET not in repr(req)


def test_usage_counts_are_whole_and_cached_is_part_of_input() -> None:
    assert Usage(10, 2, 10).cached == 10
    for bad in [(-1, 0, 0), (1, -1, 0), (1, 0, 2), (True, 0, 0), (1.0, 0, 0)]:
        with pytest.raises(ValueError, match="usage"):
            Usage(*bad)  # type: ignore[arg-type]


def _response(**changes: Any) -> LLMResponse:
    fields: dict[str, Any] = {
        "text": "{}",
        "structured": parse_structured("{}"),
        "usage": Usage(1, 1),
        "stop_reason": StopReason.COMPLETE,
        "provider_request_id": "req_1",
        "latency_ms": 0,
    }
    fields.update(changes)
    return LLMResponse(**fields)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", None),
        ("structured", {"a": 1}),
        ("usage", (1, 1, 0)),
        ("stop_reason", "complete"),
        ("provider_request_id", ""),
        ("latency_ms", -1),
        ("latency_ms", 1.5),
    ],
)
def test_a_response_checks_its_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each field has its own message
        _response(**{field: value})


@pytest.mark.parametrize("stop", [StopReason.MAX_TOKENS, StopReason.REFUSED])
def test_only_a_complete_answer_has_structured_output(stop: StopReason) -> None:
    with pytest.raises(ValueError, match="structured"):
        _response(stop_reason=stop)
    assert _response(stop_reason=stop, structured=None).structured is None


def test_parse_structured_reads_one_json_object_read_only() -> None:
    value = parse_structured(' {"a": [1, {"b": null}], "c": 1.5} \n')
    assert thaw(value) == {"a": [1, {"b": None}], "c": 1.5}
    with pytest.raises(TypeError):
        value["a"] = 2  # type: ignore[index]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "1",
        '"text"',
        "null",
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": -Infinity}',
        '{"a": 1e999}',
        '{"a": 1, "a": 2}',
        '{"a": 1} {"b": 2}',
        '{"a": 1',
        "[" * 100_000 + "]" * 100_000,
        '{"a":' * 100_000 + "1" + "}" * 100_000,
    ],
)
def test_parse_structured_refuses_anything_else(text: str) -> None:
    with pytest.raises(LLMError) as raised:
        parse_structured(text)
    assert raised.value.kind is ErrorKind.BAD_RESPONSE
    assert raised.value.__cause__ is None


def test_llm_error_messages_are_fixed_text_and_retryable_is_by_kind() -> None:
    messages = {str(LLMError(kind)) for kind in ErrorKind}
    assert len(messages) == len(ErrorKind)
    assert {kind for kind in ErrorKind if LLMError(kind).retryable} == RETRYABLE
    assert {ErrorKind.RATE_LIMITED, ErrorKind.UNAVAILABLE, ErrorKind.TIMEOUT} == RETRYABLE
    with pytest.raises(TypeError):
        LLMError("timeout")  # type: ignore[arg-type]


def test_answer_schema_is_the_cited_answer_model_and_read_only() -> None:
    assert thaw(ANSWER_SCHEMA) == StructuredAnswer.model_json_schema()
    assert isinstance(ANSWER_SCHEMA, Mapping)
    with pytest.raises(TypeError):
        ANSWER_SCHEMA["type"] = "array"  # type: ignore[index]
    # Strict structured output on both providers: every field required, no others allowed.
    schema = StructuredAnswer.model_json_schema()
    for part in [schema, *schema["$defs"].values()]:
        assert part["additionalProperties"] is False
        assert set(part["required"]) == set(part["properties"])


@pytest.mark.parametrize(
    "answer",
    [
        {"answer": [], "insufficient_evidence": "no", "missing": None},
        {"answer": [], "insufficient_evidence": False},
        {"answer": [], "insufficient_evidence": False, "missing": None, "extra": 1},
        {
            "answer": [{"text": SECRET, "sources": "S1", "quote": None}],
            "insufficient_evidence": False,
            "missing": None,
        },
        {
            "answer": [{"text": SECRET, "sources": ["S1"], "quote": 5}],
            "insufficient_evidence": False,
            "missing": None,
        },
        {
            "answer": [{"text": SECRET, "sources": [1], "quote": None}],
            "insufficient_evidence": False,
            "missing": None,
        },
    ],
)
def test_read_answer_refuses_any_other_shape_without_quoting_it(answer: dict[str, Any]) -> None:
    with pytest.raises(LLMError) as raised:
        read_answer(answer)
    assert raised.value.kind is ErrorKind.BAD_RESPONSE
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None


def test_read_answer_keeps_an_invented_label_for_the_citation_validator() -> None:
    answer = read_answer(
        {
            "answer": [{"text": "Due Friday.", "sources": ["S99"], "quote": None}],
            "insufficient_evidence": False,
            "missing": None,
        }
    )
    assert answer.answer[0].sources == ["S99"]


SDKS = frozenset({"anthropic", "openai"})
FRAMEWORKS = frozenset({"langchain", "langchain_core", "llama_index", "litellm"})


def _forbidden_imports(source: str, *, in_providers: bool) -> list[str]:
    """Modules ``source`` imports that it may not: a framework anywhere (ADR 0009), a provider
    SDK outside ``vault.ai.providers`` (ADR 0010)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in FRAMEWORKS or (top in SDKS and not in_providers):
                found.append(name)
    return found


@pytest.mark.parametrize(
    ("source", "in_providers", "found"),
    [
        ("import anthropic", False, ["anthropic"]),
        ("from openai.types import Response", False, ["openai.types"]),
        ("import os, openai as o", False, ["openai"]),
        ("import anthropic", True, []),
        ("from langchain_core import x", True, ["langchain_core"]),
        ("from . import openai", False, []),
        ("import anthropical", False, []),
    ],
)
def test_the_import_rule_finds_what_it_should(
    source: str, in_providers: bool, found: list[str]
) -> None:
    assert _forbidden_imports(source, in_providers=in_providers) == found


def test_only_the_provider_package_may_import_a_provider_sdk() -> None:
    """ADR 0009 and ADR 0010: the AI service's providers are the only path to a provider."""
    root = Path(vault.__file__).parent
    providers = root / "ai" / "providers"
    offenders = [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if _forbidden_imports(
            path.read_text(encoding="utf-8"), in_providers=providers in path.parents
        )
    ]
    assert offenders == []
