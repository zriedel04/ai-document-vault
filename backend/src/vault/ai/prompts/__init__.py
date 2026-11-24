"""Versioned prompt files (plan section 9, ADR 0009): ``answer.v1.md`` and later ones.

A prompt is a Markdown file in this package, loaded as package data, and named by its id
(``answer.v1``: the task, a dot, ``v`` and a whole number). The id is stored with every result,
so it must name exactly one text: :data:`PROMPTS` records the SHA-256 of each released file, and
:func:`load_prompt` refuses a file whose bytes no longer match. Changing a prompt therefore means
a new file and a new id (``answer.v2.md``), never an edit of a released one; a test fails first.

The loader reads only the ids listed in :data:`PROMPTS`, never a path built from a caller's
value. A file must be UTF-8 without a byte order mark, use LF line breaks only (``.gitattributes``
checks text out with LF on every platform; a CR means the bytes are not the released ones anyway)
and hold a character other than white space. Every refusal is a :class:`PromptError` with fixed
text naming at most the id, which is a constant of this package. Nothing here logs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Final

PROMPT_ID: Final = re.compile(r"[a-z][a-z_]*\.v[1-9][0-9]*")

# The SHA-256 of each released prompt file's bytes. A new version is a new file and a new row.
PROMPTS: Final = MappingProxyType(
    {
        "answer.v1": "5e272b95041e0378a7fdaab8e1b0f5c41c4e00448d929a95970e0e12ccc8b006",
    }
)


class PromptError(Exception):
    """A prompt that is not registered or whose file is not the released text."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """A loaded prompt: its id (stored with every result), its text and the text's SHA-256."""

    id: str
    text: str = field(repr=False)
    sha256: str


def load_prompt(prompt_id: str) -> Prompt:
    """The prompt named ``prompt_id`` (for example ``answer.v1``). Raises :class:`PromptError`."""
    if not isinstance(prompt_id, str) or prompt_id not in PROMPTS:
        msg = "no prompt has that id"
        raise PromptError(msg)
    return _load(prompt_id)


@cache
def _load(prompt_id: str) -> Prompt:
    try:
        data = resources.files("vault.ai.prompts").joinpath(f"{prompt_id}.md").read_bytes()
    except OSError:
        msg = f"the prompt file for {prompt_id} cannot be read"
        raise PromptError(msg) from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != PROMPTS[prompt_id]:
        msg = f"the prompt file for {prompt_id} is not the released text; add a new version"
        raise PromptError(msg)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        msg = f"the prompt file for {prompt_id} is not UTF-8"
        raise PromptError(msg) from None
    if text.startswith("\ufeff") or "\r" in text or not text.strip():
        msg = f"the prompt file for {prompt_id} must be UTF-8 text with LF line breaks"
        raise PromptError(msg)
    return Prompt(id=prompt_id, text=text, sha256=digest)
