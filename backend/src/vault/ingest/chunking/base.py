"""What a chunker produces: chunks of a version's pages, each traceable to its page and offsets.

A chunk is the unit of retrieval (plan section 6, ``chunks``). Every chunk names the page it comes
from and the span ``page.text[char_start:char_end]`` it covers, and its ``text`` is exactly that
span, so a citation can always be traced back to the page and highlighted there.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    """One chunk of a document version.

    ``index`` counts the version's chunks from 0 in reading order. ``page_start`` and ``page_end``
    are page numbers (from 1); ``char_start`` is an offset into the text of ``page_start`` and
    ``char_end`` an offset into the text of ``page_end``. ``section_path`` holds the titles of the
    headings the chunk lies under, outermost first. ``token_count`` is the chunker's own count of
    the text's tokens (an approximation, see ``structural_v1.count_tokens``).
    """

    index: int
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    char_start: int
    char_end: int
    text: str
    token_count: int
