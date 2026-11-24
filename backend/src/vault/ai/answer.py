"""The structured cited answer a model returns for Ask the Vault (ADR 0011, plan section 11).

The shape is defined once, by the citation validator's :class:`StructuredAnswer`
(:mod:`vault.answers.citations`): segments, each with the source labels that support it (``S1``...)
and a short quote, plus an explicit ``insufficient_evidence`` flag and a note on what is missing.
This module only puts it in the provider's terms: :data:`ANSWER_SCHEMA` is that model's JSON
schema, read-only, and :func:`read_answer` reads a provider's structured output with the
validator's own reader. Nothing here defines a field, so the schema sent to a provider and the
validator cannot disagree.

Whether a label was given, and whether a quote is in its chunk, is
:func:`vault.answers.citations.validate_answer`: an invented label is a valid answer here and is
marked unsupported there.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from vault.ai.providers.base import ErrorKind, JSONValue, LLMError, parse_structured, thaw
from vault.answers.citations import (
    AnswerFormatError,
    StructuredAnswer,
    answer_json_schema,
    read_structured,
)

# Read-only, like every schema a request holds (``parse_structured`` freezes it).
ANSWER_SCHEMA: Final[Mapping[str, JSONValue]] = parse_structured(json.dumps(answer_json_schema()))


def read_answer(structured: Mapping[str, object]) -> StructuredAnswer:
    """The cited answer in ``structured`` (an ``LLMResponse.structured``).

    Anything the validator's reader refuses is :attr:`ErrorKind.BAD_RESPONSE`.
    """
    try:
        return read_structured(thaw(structured))
    except AnswerFormatError:
        raise LLMError(ErrorKind.BAD_RESPONSE) from None
