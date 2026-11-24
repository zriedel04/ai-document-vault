"""One answer shape (ADR 0011, plan section 11): the schema sent to a provider and the citation
validator's readers come from the same definition, and accept exactly the same answers."""

from __future__ import annotations

import ast
import json
import random
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

import vault
from vault.ai import answer as ai_answer
from vault.ai.answer import ANSWER_SCHEMA, read_answer
from vault.ai.providers.base import (
    LLMError,
    LLMRequest,
    Message,
    Role,
    SourceBlock,
    Task,
    parse_structured,
    thaw,
)
from vault.ai.providers.fake import FakeProvider
from vault.answers import citations
from vault.answers.citations import (
    MAX_ANSWER_CHARS,
    MAX_LABEL_CHARS,
    MAX_NOTE_CHARS,
    MAX_QUOTE_CHARS,
    MAX_SEGMENT_CHARS,
    MAX_SEGMENTS,
    MAX_SOURCES_PER_SEGMENT,
    AnswerFormatError,
    SegmentStatus,
    SentSource,
    SourceMap,
    StructuredAnswer,
    parse_answer,
    validate_answer,
)

SCHEMA: dict[str, Any] = thaw(ANSWER_SCHEMA)  # type: ignore[assignment]

# Every keyword the schema may use. The checker below implements these and nothing else, so a
# keyword added to the model (a pattern, a minimum length) fails here until it is checked too.
KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "items",
        "maxItems",
        "maxLength",
        "properties",
        "required",
        "title",
        "type",
    }
)
TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _keywords(node: object) -> set[str]:
    """Every keyword used anywhere in the schema (property names are not keywords)."""
    if not isinstance(node, dict):
        return set()
    found = set(node)
    for key, value in node.items():
        if key in {"properties", "$defs"}:
            for sub in value.values():
                found |= _keywords(sub)
        elif key == "items":
            found |= _keywords(value)
        elif key == "anyOf":
            for sub in value:
                found |= _keywords(sub)
    return found


def _valid(value: object, node: Mapping[str, Any], root: Mapping[str, Any] = SCHEMA) -> bool:
    """Whether ``value`` is valid under ``node``: a JSON Schema check written from the
    specification for the keywords in ``KEYWORDS``, independent of pydantic."""
    if "$ref" in node:
        prefix = "#/$defs/"
        assert node["$ref"].startswith(prefix)
        return _valid(value, root["$defs"][node["$ref"][len(prefix) :]], root)
    if "anyOf" in node and not any(_valid(value, sub, root) for sub in node["anyOf"]):
        return False
    if "type" in node and not TYPES[node["type"]](value):
        return False
    if isinstance(value, str) and "maxLength" in node and len(value) > node["maxLength"]:
        return False
    if isinstance(value, list):
        if "maxItems" in node and len(value) > node["maxItems"]:
            return False
        if "items" in node and not all(_valid(item, node["items"], root) for item in value):
            return False
    if isinstance(value, dict):
        properties = node.get("properties", {})
        if any(key not in value for key in node.get("required", [])):
            return False
        if node.get("additionalProperties") is False and any(k not in properties for k in value):
            return False
        if not all(
            _valid(value[key], sub, root) for key, sub in properties.items() if key in value
        ):
            return False
    return True


def _accepted_by_read_answer(value: object) -> bool:
    try:
        read_answer(parse_structured(json.dumps(value)))
    except LLMError:
        return False
    return True


def _accepted_by_parse_answer(value: object) -> bool:
    raw = json.dumps(value)
    assert len(raw) <= MAX_ANSWER_CHARS  # the generator stays within the text bound
    try:
        parse_answer(raw)
    except AnswerFormatError:
        return False
    return True


def _text(rng: random.Random, limit: int) -> object:
    return rng.choice(["", " ", "S1", "[S3]", "x" * limit, "x" * (limit + 1), "é" * limit])


def _wrong(rng: random.Random) -> object:
    return rng.choice([None, 0, 1.5, True, "true", [], {}, ["S1"]])


def _field(rng: random.Random, good: Callable[[], object]) -> object:
    return good() if rng.random() < 0.8 else _wrong(rng)


def _segment(rng: random.Random) -> object:
    if rng.random() < 0.03:
        return _wrong(rng)
    labels = rng.choice([0, 1, 2, MAX_SOURCES_PER_SEGMENT, MAX_SOURCES_PER_SEGMENT + 1])
    segment: dict[str, object] = {
        "text": _field(rng, lambda: _text(rng, MAX_SEGMENT_CHARS)),
        "sources": _field(
            rng,
            lambda: [
                _field(rng, lambda: _text(rng, MAX_LABEL_CHARS)) if rng.random() < 0.3 else "S1"
                for _ in range(labels)
            ],
        ),
        "quote": _field(rng, lambda: None if rng.random() < 0.3 else _text(rng, MAX_QUOTE_CHARS)),
    }
    return _shuffle_keys(rng, segment)


def _shuffle_keys(rng: random.Random, value: dict[str, object]) -> dict[str, object]:
    if rng.random() < 0.05:
        del value[rng.choice(list(value))]
    if rng.random() < 0.05:
        value[rng.choice(["page", "chunk_id", "Quote"])] = "x"
    return value


def _answer(rng: random.Random) -> object:
    if rng.random() < 0.02:
        return _wrong(rng)
    if rng.random() < 0.05:
        # The segment count at and past its bound, with small segments to stay within the text.
        count = rng.choice([MAX_SEGMENTS, MAX_SEGMENTS + 1])
        segments: object = [{"text": "t", "sources": [], "quote": None}] * count
    else:
        segments = _field(rng, lambda: [_segment(rng) for _ in range(rng.randrange(3))])
    answer: dict[str, object] = {
        "answer": segments,
        "insufficient_evidence": _field(rng, lambda: rng.choice([True, False])),
        "missing": _field(rng, lambda: None if rng.random() < 0.3 else _text(rng, MAX_NOTE_CHARS)),
    }
    return _shuffle_keys(rng, answer)


def test_the_provider_schema_is_the_validators_model() -> None:
    assert StructuredAnswer.model_json_schema() == SCHEMA
    assert citations.answer_json_schema() == SCHEMA


def test_the_provider_module_defines_no_shape_of_its_own() -> None:
    tree = ast.parse(Path(ai_answer.__file__).read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    answer = read_answer(
        {"answer": [], "insufficient_evidence": True, "missing": "No passage gives the date."}
    )
    assert type(answer) is StructuredAnswer


def test_only_one_class_in_the_package_declares_the_answer_fields() -> None:
    root = Path(vault.__file__).parent
    declaring = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            names = {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
            if "insufficient_evidence" in names and "answer" in names:
                declaring.append((path.relative_to(root).as_posix(), node.name))
    assert declaring == [("answers/citations.py", "StructuredAnswer")]


def test_the_schema_uses_only_checked_keywords_and_no_descriptions() -> None:
    # A description would be text for the model, which belongs to the prompt and not to the code.
    assert _keywords(SCHEMA) <= KEYWORDS


def test_every_field_is_required_and_no_other_allowed() -> None:
    segment = SCHEMA["$defs"]["AnswerSegment"]
    for part in (SCHEMA, segment):
        assert part["type"] == "object"
        assert part["additionalProperties"] is False
        assert part["required"] == list(part["properties"])
    assert list(SCHEMA["properties"]) == ["answer", "insufficient_evidence", "missing"]
    assert list(segment["properties"]) == ["text", "sources", "quote"]


def test_each_field_has_the_validators_type_nullability_and_bound() -> None:
    segment = SCHEMA["$defs"]["AnswerSegment"]["properties"]
    answer = SCHEMA["properties"]
    assert segment["text"] == {"title": "Text", "type": "string", "maxLength": MAX_SEGMENT_CHARS}
    # A label is any short text: an invented one is the validator's finding, not a refusal.
    assert segment["sources"] == {
        "title": "Sources",
        "type": "array",
        "items": {"type": "string", "maxLength": MAX_LABEL_CHARS},
        "maxItems": MAX_SOURCES_PER_SEGMENT,
    }
    assert segment["quote"] == {
        "title": "Quote",
        "anyOf": [{"type": "string", "maxLength": MAX_QUOTE_CHARS}, {"type": "null"}],
    }
    assert answer["answer"] == {
        "title": "Answer",
        "type": "array",
        "items": {"$ref": "#/$defs/AnswerSegment"},
        "maxItems": MAX_SEGMENTS,
    }
    assert answer["insufficient_evidence"] == {"title": "Insufficient Evidence", "type": "boolean"}
    assert answer["missing"] == {
        "title": "Missing",
        "anyOf": [{"type": "string", "maxLength": MAX_NOTE_CHARS}, {"type": "null"}],
    }


@pytest.mark.parametrize(
    "value",
    [
        # The answer of the bug: a normal answer with nothing missing.
        {
            "answer": [{"text": "Due Friday.", "sources": ["S1"], "quote": "due Friday"}],
            "insufficient_evidence": False,
            "missing": None,
        },
        {
            "answer": [{"text": "Due Friday.", "sources": ["S1"], "quote": None}],
            "insufficient_evidence": False,
            "missing": None,
        },
        {"answer": [], "insufficient_evidence": True, "missing": None},
        {"answer": [], "insufficient_evidence": True, "missing": "No date is given."},
        {
            "answer": [{"text": "t", "sources": ["[S3]", "s3"], "quote": ""}],
            "insufficient_evidence": False,
            "missing": "",
        },
    ],
)
def test_answers_valid_under_the_schema_pass_both_readers(value: dict[str, object]) -> None:
    assert _valid(value, SCHEMA)
    assert _accepted_by_read_answer(value)
    assert _accepted_by_parse_answer(value)


def test_schema_and_readers_agree_on_generated_answers() -> None:
    rng = random.Random(20261124)  # noqa: S311 - test data, not security
    seen = {True: 0, False: 0}
    for _ in range(4000):
        value = _answer(rng)
        expected = _valid(value, SCHEMA)
        assert _accepted_by_read_answer(value) is expected, value
        assert _accepted_by_parse_answer(value) is expected, value
        seen[expected] += 1
    # Both sides of the boundary are exercised, not only one.
    assert min(seen.values()) > 500


@pytest.mark.parametrize(
    "raw",
    [
        '{"answer": [], "insufficient_evidence": true, "missing": "\\ud800"}',
        '{"answer": [{"text": "\\udfff", "sources": [], "quote": null}],'
        ' "insufficient_evidence": false, "missing": null}',
    ],
)
def test_a_lone_surrogate_is_refused_by_both_readers(raw: str) -> None:
    # The one rule JSON Schema cannot state: text a UTF-8 store can hold. Both readers apply it.
    with pytest.raises(AnswerFormatError):
        parse_answer(raw)
    with pytest.raises(LLMError):
        read_answer(parse_structured(raw))


def _request(texts: list[str]) -> LLMRequest:
    return LLMRequest(
        task=Task.ANSWER,
        model="fake-1",
        system="Answer from the sources.",
        messages=(Message(Role.USER, "What is due?"),),
        sources=tuple(
            SourceBlock(f"S{i}", f"Doc {i}", text) for i, text in enumerate(texts, start=1)
        ),
        output_schema=ANSWER_SCHEMA,
        max_output_tokens=4000,
    )


def test_the_fake_providers_answer_passes_the_validator() -> None:
    texts = ["The prototype is due on 15 October. Late work loses points.", "Room 204 is open."]
    response = FakeProvider().generate(_request(texts))
    assert response.structured is not None
    parsed = parse_answer(response.text)
    assert parsed == read_answer(response.structured)
    assert parsed.missing is None
    sources = SourceMap(
        [
            SentSource(
                chunk_id=i,
                version_id=f"01J9Z3K4M5N6P7Q8R9S{i:07d}",
                document_id=f"01J9Z3K4M5N6P7Q8R9T{i:07d}",
                page_start=1,
                page_end=1,
                text=t,
            )
            for i, t in enumerate(texts, start=1)
        ]
    )
    result = validate_answer(parsed, sources)
    assert result.findings == ()
    assert [segment.status for segment in result.segments] == [SegmentStatus.SUPPORTED] * 2


def test_the_fake_providers_abstention_passes_the_validator() -> None:
    response = FakeProvider().generate(_request([]))
    parsed = parse_answer(response.text)
    assert parsed == read_answer(response.structured or {})
    result = validate_answer(parsed, SourceMap([]))
    assert result.insufficient_evidence is True
    assert result.findings == ()
