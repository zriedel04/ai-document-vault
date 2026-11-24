"""The structured cited answer a model returns for Ask the Vault (ADR 0011, plan section 11).

The answer is a list of segments, each with the source labels that support it (``S1``...) and a
short quote, plus an explicit ``insufficient_evidence`` flag and a note on what is missing. The
schema sent to the provider (:data:`ANSWER_SCHEMA`) and the reader (:func:`read_answer`) come from
the one model below, so they cannot disagree. Every field is required and no other field is
allowed, which is what strict structured output on both providers expects; "no quote" and "nothing
missing" are ``null``.

This module only reads the shape. Whether a label was given, and whether a quote is in its chunk,
is the server's citation validation (ADR 0011), which comes with the prompt in a later change: an
invented label is a valid answer here and is marked unsupported there.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from vault.ai.providers.base import ErrorKind, JSONValue, LLMError, parse_structured, thaw


class _Strict(BaseModel):
    # hide_input_in_errors: a ValidationError never repeats the model's text.
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


class AnswerSegment(_Strict):
    text: str
    sources: list[str]
    quote: str | None


class CitedAnswer(_Strict):
    answer: list[AnswerSegment]
    insufficient_evidence: bool
    missing: str | None


# Read-only, like every schema a request holds (``parse_structured`` freezes it).
ANSWER_SCHEMA: Final[Mapping[str, JSONValue]] = parse_structured(
    json.dumps(CitedAnswer.model_json_schema())
)


def read_answer(structured: Mapping[str, object]) -> CitedAnswer:
    """The cited answer in ``structured`` (an ``LLMResponse.structured``).

    Anything that does not fit the schema exactly is :attr:`ErrorKind.BAD_RESPONSE`.
    """
    try:
        return CitedAnswer.model_validate(thaw(structured))
    except ValidationError:
        raise LLMError(ErrorKind.BAD_RESPONSE) from None
