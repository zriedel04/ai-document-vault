"""Plain text and Markdown: decode the bytes, then find Markdown's headings.

Decoding follows the validator's rule (``vault.ingest.validate``) and then goes further, because
validation read only the head of the file:

- A byte order mark names the encoding (UTF-8, UTF-16 or UTF-32), and the whole file must be valid
  in it. The mark itself is not part of the text.
- Without a mark the file is UTF-8 if it is valid UTF-8 from end to end, and otherwise the fallback
  code page: Windows-1252 by default, the code page Windows writes for Western languages (Notepad's
  "ANSI" before Windows 10), or another Windows code page the caller passes (``fallback``, one of
  ``FALLBACK_ENCODINGS``; Windows-1251 for Cyrillic, say). That is a fixed choice, not a guess per
  file: the result records which encoding was used, and a file in a code page other than the
  fallback decodes to the wrong letters. Automatic detection was tried and left out: on short
  notes charset-normalizer read Western text as Central European (``crème`` as ``crčme``) with full
  confidence. A file that mixes UTF-8 with another encoding falls back as a whole.
- The decoded text must pass the validator's text rule (``reads_as_text``) as a whole: no NUL and
  only a few control characters. A file that turns binary after its head is refused here.
- Line breaks are stored as ``\\n`` (Windows ``\\r\\n`` and old Mac ``\\r`` are converted), so
  offsets do not depend on the editor that wrote the file. Nothing else is changed.

A text file is one page. Markdown headings are found by markdown-it-py with its CommonMark preset
(ADR 0018): ``#`` to ``######`` headings and paragraphs underlined with ``===`` (level 1) or ``---``
(level 2), with code blocks, HTML blocks and every container rule handled by the library. Only
headings at the top level are recorded: a heading inside a block quote or a list item is not a
section heading of the document. A YAML front matter block at the very start is skipped before
parsing. markdown-it-py departs from the specification on some link reference definitions (the
ADR has the details); that can change only where a section starts.
"""

from __future__ import annotations

import codecs
import itertools
import re
from typing import Final

from markdown_it import MarkdownIt

from vault.ingest.extractors import Extraction, Heading, NotTextError, Page
from vault.ingest.validate import TEXT_BOMS, reads_as_text

VERSION: Final = "text-1"
FALLBACK_ENCODING: Final = "cp1252"
# The Windows "ANSI" code pages, one of which the owner may choose as the fallback: every code page
# Microsoft's code page table marks ANSI, Korean Johab (1361) included.
FALLBACK_ENCODINGS: Final = frozenset(
    {"cp874", "cp932", "cp936", "cp949", "cp950", "cp1361", *(f"cp{n}" for n in range(1250, 1259))}
)

_LINE_BREAK: Final = re.compile(r"\r\n?")
_FRONT_MATTER_OPEN: Final = "---"
_FRONT_MATTER_CLOSE: Final = ("---", "...")
# CommonMark preset (ADR 0018). Only the token stream is used; nothing is rendered.
_MARKDOWN: Final = MarkdownIt("commonmark")


def decode(data: bytes, fallback: str = FALLBACK_ENCODING) -> tuple[str, str]:
    """The text in ``data`` with ``\\n`` line breaks, and the encoding it was decoded with.

    ``fallback`` is the code page for bytes that carry no mark and are not UTF-8; anything but one
    of ``FALLBACK_ENCODINGS`` is a ``ValueError``.

    Raises ``NotTextError`` if the bytes are not valid in the encoding their mark names, or if the
    decoded text is not text by the validator's rule.
    """
    if fallback not in FALLBACK_ENCODINGS:
        msg = "The fallback must be a Windows code page from FALLBACK_ENCODINGS."
        raise ValueError(msg)
    for bom, encoding in TEXT_BOMS:
        if data.startswith(bom):
            text = _strict(data[len(bom) :], encoding)
            break
    else:
        try:
            encoding = "utf-8"
            text = data.decode(encoding)
        except UnicodeDecodeError:
            encoding = fallback
            text = _strict(data, encoding)
    if text is None or not reads_as_text(text):
        raise NotTextError
    return _LINE_BREAK.sub("\n", text), codecs.lookup(encoding).name


def _strict(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


def extract(data: bytes, *, markdown: bool, fallback: str = FALLBACK_ENCODING) -> Extraction:
    """One page holding the decoded text, with its headings when ``markdown`` is true."""
    text, encoding = decode(data, fallback)
    headings = markdown_headings(text) if markdown else ()
    return Extraction(VERSION, (Page(1, text, headings),), encoding)


def markdown_headings(text: str) -> tuple[Heading, ...]:
    """The top-level headings of Markdown ``text`` (``\\n`` line breaks), in order.

    A heading starts at the start of its first line and ends at the end of its last line (the
    underline of a setext heading), without the line break. A setext heading under link reference
    definitions starts at the first line after them.
    """
    lines = text.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    skipped = _after_front_matter(lines)
    tokens = _MARKDOWN.parse("\n".join(lines[skipped:]))
    found: list[Heading] = []
    for opening, inline in itertools.pairwise(tokens):
        if opening.type != "heading_open" or opening.level != 0 or opening.map is None:
            continue
        first, after = (line + skipped for line in opening.map)
        if opening.markup in ("=", "-"):
            title = " ".join(part.strip() for part in inline.content.split("\n"))
        else:
            title = inline.content.strip()
        end = starts[after - 1] + len(lines[after - 1])
        found.append(Heading(int(opening.tag[1:]), title, starts[first], end))
    return tuple(found)


def _after_front_matter(lines: list[str]) -> int:
    """The index of the first line after a YAML front matter block, or 0 if there is none."""
    if not lines or lines[0].rstrip() != _FRONT_MATTER_OPEN:
        return 0
    for index in range(1, len(lines)):
        if lines[index].rstrip() in _FRONT_MATTER_CLOSE:
            return index + 1
    return 0
