"""The Ask the Vault request: prompt v1, the question and the sources (plan sections 9 to 11).

:func:`build_answer_request` turns the owner's question and the retrieved passages
(:class:`SourceBlock`, labelled ``S1`` to ``Sn`` in order) into an :class:`LLMRequest` for
:attr:`Task.ANSWER`, and returns it with the id of the prompt it used (``answer.v1``), which is
stored with the result. It is pure: no network, no database, no logging, no clock. The request
holds:

- ``system``: the text of ``prompts/answer.v1.md`` exactly, the same for every question, so a
  provider's prompt cache can reuse it (it sits first in every request). The rules live only
  there (ADR 0010, structural separation).
- ``messages``: one user message, the question as typed.
- ``sources``: the passages as data, not as a formatted string (plan section 9), so the server's
  label map and an adapter's native document features see them as they are.
- ``output_schema``: the cited-answer schema (``vault.ai.answer.ANSWER_SCHEMA``, ADR 0011).

:func:`render_user_turn` is the one text form of the request's user turn, which an adapter sends
after the system prompt: a ``<sources>`` block of ``<source id="S3" doc="..." page="4">``
elements, then a ``<question>`` block. It is a function of the request alone, so every adapter
writes the same bytes and a test can see them.

**Escaping.** Source text, titles and the question are untrusted (a document may hold text written
to steer a model), so nothing in them can close or open a block or forge a label:

- In text (a source's body and the question), ``&``, ``<`` and ``>`` become ``&amp;``, ``&lt;``
  and ``&gt;``, and every character in :data:`LOOKALIKES` becomes a hexadecimal character
  reference (``&#xff1c;``). The lookalikes are the characters that normalise (NFKC) to ``<``,
  ``>`` or ``&`` (the full-width and small forms) and the angle brackets, which a model may read
  as tag delimiters even though an XML parser would not; a test derives the set from the Unicode
  database. A CR becomes ``&#xd;`` (an XML reader would turn it into a LF). Everything else,
  LF included, is sent as it is.
- In an attribute (a title, the label and the pages), the same, and also ``"`` and the characters
  that normalise to it, and every control, line and paragraph separator character, so a title stays
  one attribute value on one line.

For input made of characters XML 1.0 allows (all but the C0 controls other than tab, LF and CR,
U+FFFE and U+FFFF), the result is well-formed XML whose element texts and attributes decode back
to the exact input; the tests parse it with an XML parser to check that. Other characters are sent
as they are in text and as references in attributes, and close nothing either way. The model is
told what the references stand for and to quote the characters themselves, because the citation
validator matches a quote against the passage as stored, not as escaped. A string that is not
valid Unicode (a lone surrogate) cannot be sent and raises ``ValueError``; so do the checks of
:class:`LLMRequest`.
Every ``ValueError`` has fixed text that repeats nothing from the input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vault.ai.answer import ANSWER_SCHEMA
from vault.ai.prompts import load_prompt
from vault.ai.providers.base import Effort, LLMRequest, Message, Role, SourceBlock, Task

ANSWER_PROMPT_ID: Final = "answer.v1"

# The characters, other than ``<``, ``>`` and ``&``, whose NFKC form holds one of them, and every
# character whose Unicode name holds "ANGLE BRACKET" (Unicode 15.0.0, Python 3.12). A test scans
# every code point and fails if this set and the rule disagree.
LOOKALIKES: Final = frozenset(
    "\u2329\u232a\u276c\u276d\u2770\u2771\u27e8\u27e9\u27ea\u27eb\u2991\u2992\u29fc\u29fd"
    "\u3008\u3009\u300a\u300b\ufe3d\ufe3e\ufe3f\ufe40\ufe60\ufe64\ufe65\uff06\uff1c\uff1e"
)
# Characters that normalise (NFKC) to ``"``: only the full-width quotation mark.
QUOTE_LOOKALIKES: Final = frozenset("\uff02")

_NAMED: Final = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _reference(char: str) -> str:
    return f"&#x{ord(char):x};"


def _attribute_only(char: str) -> bool:
    """A character a title may not hold as it is: a control, line or paragraph separator."""
    code = ord(char)
    return code < 0x20 or 0x7F <= code <= 0x9F or char in "\u2028\u2029"


def escape_text(value: str) -> str:
    """``value`` for the body of a block: ``&``, ``<``, ``>``, the lookalikes and CR escaped."""
    out: list[str] = []
    for char in value:
        if char in "&<>":
            out.append(_NAMED[char])
        elif char in LOOKALIKES or char == "\r":
            out.append(_reference(char))
        else:
            out.append(char)
    return "".join(out)


def escape_attribute(value: str) -> str:
    """``value`` for a double-quoted attribute: as :func:`escape_text`, and also ``"``, its
    full-width form and every control, line and paragraph separator character."""
    out: list[str] = []
    for char in value:
        if char in _NAMED:
            out.append(_NAMED[char])
        elif char in LOOKALIKES or char in QUOTE_LOOKALIKES or _attribute_only(char):
            out.append(_reference(char))
        else:
            out.append(char)
    return "".join(out)


def _unicode(value: str, what: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        msg = f"{what} is not valid Unicode text"
        raise ValueError(msg) from None


def _pages(source: SourceBlock) -> str | None:
    if source.page_start is None or source.page_end is None:
        return None
    if source.page_start == source.page_end:
        return str(source.page_start)
    return f"{source.page_start}-{source.page_end}"


def render_source(source: SourceBlock) -> str:
    """One ``<source id="S3" doc="..." page="4">`` element; ``page`` is left out without pages."""
    attributes = f'id="{escape_attribute(source.label)}" doc="{escape_attribute(source.title)}"'
    pages = _pages(source)
    if pages is not None:
        attributes += f' page="{escape_attribute(pages)}"'
    return f"<source {attributes}>\n{escape_text(source.text)}\n</source>"


def render_sources(sources: Sequence[SourceBlock]) -> str:
    """The ``<sources>`` block: every source in label order, each on its own lines."""
    inner = "".join(f"{render_source(source)}\n" for source in sources)
    return f"<sources>\n{inner}</sources>"


def render_user_turn(request: LLMRequest) -> str:
    """The user turn an adapter sends after the system prompt: the sources, then the question.

    The question is the request's last message, which :func:`build_answer_request` makes."""
    if not isinstance(request, LLMRequest):
        msg = "the request must be an LLMRequest"
        raise TypeError(msg)
    question = escape_text(request.messages[-1].content)
    return f"{render_sources(request.sources)}\n\n<question>\n{question}\n</question>"


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    """A built request and the id of the prompt it holds, to store with the result."""

    request: LLMRequest
    prompt_id: str


def build_answer_request(
    question: str,
    sources: Sequence[SourceBlock],
    *,
    model: str,
    max_output_tokens: int,
    effort: Effort | None = None,
) -> AnswerRequest:
    """The :attr:`Task.ANSWER` request for ``question`` over ``sources`` (labelled ``S1``...).

    With no sources the request is still built; the prompt tells the model to say the evidence is
    insufficient. Raises ``ValueError`` (fixed text) for a blank question, sources that are not
    ``S1`` to ``Sn`` in order, a lone surrogate, or any other rule of :class:`LLMRequest`."""
    if isinstance(sources, str | bytes) or not isinstance(sources, Sequence):
        msg = "sources must be a sequence of SourceBlock"
        raise ValueError(msg)
    items = tuple(sources)
    if not all(isinstance(item, SourceBlock) for item in items):
        msg = "sources must hold only SourceBlock"
        raise ValueError(msg)
    message = Message(Role.USER, question)
    _unicode(message.content, "the question")
    for item in items:
        _unicode(item.title, "a source's title")
        _unicode(item.text, "a source's text")
    prompt = load_prompt(ANSWER_PROMPT_ID)
    request = LLMRequest(
        task=Task.ANSWER,
        model=model,
        system=prompt.text,
        messages=(message,),
        max_output_tokens=max_output_tokens,
        sources=items,
        output_schema=ANSWER_SCHEMA,
        effort=effort,
    )
    return AnswerRequest(request=request, prompt_id=prompt.id)
