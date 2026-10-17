"""Chunker v1 (``structural-1``): structure first, then pack to 400-600 tokens (plan section 7).

The rule, in order:

1. **Sections.** A page is cut at every heading. The text before a page's first heading belongs to
   the section the previous page ended in (or to no section on the first page). A heading closes
   every open heading of its level or deeper, so the section path is the chain of titles above
   the text, outermost first. Titles are stored with control characters turned into spaces and
   runs of white space collapsed to one (``section_title``). A chunk never crosses a section or a
   page, so ``page_start`` always equals ``page_end`` in this version.
2. **Pieces.** A section is split into paragraphs (at blank lines), lines and sentences (after
   ``.``, ``!`` or ``?`` followed by white space, or after the CJK full stops U+3002,
   U+FF01 and U+FF1F). A sentence longer than the maximum is split at white space, and a
   single word longer than that into tokens.
3. **Packing.** Pieces are packed in order while the chunk stays within ``MAX_TOKENS``. When the
   next piece does not fit, the chunk ends at the strongest break (paragraph, then line, then
   sentence, then word) after which it holds at least ``MIN_TOKENS``, the latest such break on a
   tie; if no break leaves that many, it ends where the next piece did not fit. The last chunk of
   a section is whatever is left.
4. **Overlap.** When a section continues in a new chunk, that chunk starts again at the previous
   chunk's last piece, if the previous chunk had more than one piece, that piece ends at a
   sentence, line or paragraph break (it is not part of a cut sentence or word), it has at most
   ``OVERLAP_TOKENS`` tokens and it fits together with the next piece.
5. **Offsets.** Each chunk is trimmed of white space at both ends, and its text is exactly
   ``page.text[char_start:char_end]``. A page or section with no visible text gives no chunk.

The plan asks for chunks of roughly 400-600 tokens without naming a tokenizer, and the project has
no tokenizer dependency, so ``count_tokens`` approximates one: every run of up to four letters or
digits is a token, and so is every other visible character. Sub-word tokenizers average about
four characters of English per token, so this lands close for English and other languages written
with spaces; for Chinese or Japanese it counts about one token per four characters, which is fewer
than a real tokenizer would, so their chunks hold more characters. Any change to this rule, the
sizes or the overlap is a new chunker version, which re-chunks every document.

Page text and heading titles are the document's own words; nothing here logs them, and errors name
a page by its number only (``vault.db.pages.check_pages``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from vault.db.pages import PageText, check_pages
from vault.ingest.chunking.base import Chunk

CHUNKER_VERSION: Final = "structural-1"
MAX_TOKENS: Final = 600
MIN_TOKENS: Final = 400
OVERLAP_TOKENS: Final = 60

TOKEN: Final = re.compile(r"\w{1,4}|[^\w\s]")

# How strong the break after a piece is: a chunk prefers to end at the strongest one.
_HARD: Final = 0
_WORD: Final = 1
_SENTENCE: Final = 2
_LINE: Final = 3
_PARAGRAPH: Final = 4

# Always applied, in this order, so the pieces are at most sentences.
_STRUCTURE: Final = (
    (re.compile(r"\n\s*\n"), _PARAGRAPH),
    (re.compile(r"\n"), _LINE),
    (re.compile(r"(?<=[.!?])\s+|(?<=[\u3002\uff01\uff1f])\s*"), _SENTENCE),
)
_WHITE_SPACE: Final = re.compile(r"\s+")

# The C0 and C1 control characters (Unicode category Cc).
_CONTROLS_TO_SPACE: Final = dict.fromkeys((*range(0x20), *range(0x7F, 0xA0)), " ")


def count_tokens(text: str, start: int = 0, end: int | None = None) -> int:
    """The approximate token count of ``text[start:end]`` (see the module docstring)."""
    return len(TOKEN.findall(text, start, len(text) if end is None else end))


def section_title(title: str) -> str:
    """A heading title as a section path holds it: control characters become spaces, and white
    space is collapsed to single spaces and trimmed."""
    return " ".join(title.translate(_CONTROLS_TO_SPACE).split())


@dataclass(frozen=True)
class _Piece:
    start: int
    end: int
    tokens: int
    break_after: int


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _split_at(
    pattern: re.Pattern[str], text: str, start: int, end: int, strength: int, last: int
) -> list[tuple[int, int, int]]:
    """``text[start:end]`` cut at each match of ``pattern``, with the strength of the break after
    each part (``last`` for the final part)."""
    parts: list[tuple[int, int, int]] = []
    position = start
    for match in pattern.finditer(text, start, end):
        if match.end() == start or match.start() == end:
            continue
        parts.append((position, match.start(), strength))
        position = match.end()
    parts.append((position, end, last))
    return parts


def _pieces(text: str, start: int, end: int, level: int, break_after: int) -> list[_Piece]:
    start, end = _trim(text, start, end)
    if start == end:
        return []
    if level < len(_STRUCTURE):
        pattern, strength = _STRUCTURE[level]
        parts = _split_at(pattern, text, start, end, strength, break_after)
    else:
        tokens = count_tokens(text, start, end)
        if tokens <= MAX_TOKENS:
            return [_Piece(start, end, tokens, break_after)]
        if level == len(_STRUCTURE):
            parts = _split_at(_WHITE_SPACE, text, start, end, _WORD, break_after)
        else:
            # One word longer than a chunk: every token is a piece of its own.
            matches = list(TOKEN.finditer(text, start, end))
            return [
                _Piece(m.start(), m.end(), 1, break_after if i == len(matches) - 1 else _HARD)
                for i, m in enumerate(matches)
            ]
    result: list[_Piece] = []
    for part_start, part_end, strength in parts:
        result.extend(_pieces(text, part_start, part_end, level + 1, strength))
    return result


def _pack(pieces: Sequence[_Piece]) -> list[tuple[int, int]]:
    """The spans of the chunks the pieces of one section make (rules 3 and 4)."""
    spans: list[tuple[int, int]] = []
    first = 0
    while first < len(pieces):
        total = 0
        after = first
        while after < len(pieces) and total + pieces[after].tokens <= MAX_TOKENS:
            total += pieces[after].tokens
            after += 1
        last = after - 1
        if after < len(pieces):
            best = -1
            running = 0
            for index in range(first, after):
                running += pieces[index].tokens
                if running >= MIN_TOKENS and pieces[index].break_after >= best:
                    best = pieces[index].break_after
                    last = index
        spans.append((pieces[first].start, pieces[last].end))
        following = last + 1
        if (
            following < len(pieces)
            and last > first  # the next chunk must bring a new piece (rule 4)
            and pieces[last].break_after >= _SENTENCE
            and pieces[last].tokens <= OVERLAP_TOKENS
            and pieces[last].tokens + pieces[following].tokens <= MAX_TOKENS
        ):
            first = last
        else:
            first = following
    return spans


def chunk_pages(pages_in: Sequence[PageText]) -> tuple[Chunk, ...]:
    """The chunks of a version's pages, in reading order, numbered from 0.

    Pure: it reads nothing and writes nothing. Raises ``ValueError`` (from
    ``vault.db.pages.check_pages``) for pages that break the page rules.
    """
    pages = check_pages(pages_in)
    open_headings: list[tuple[int, str]] = []
    chunks: list[Chunk] = []
    for page in pages:
        text = page.text
        sections: list[tuple[int, int, tuple[str, ...]]] = []
        starts = [heading.start for heading in page.headings]
        path = tuple(title for _, title in open_headings)
        sections.append((0, starts[0] if starts else len(text), path))
        for position, heading in enumerate(page.headings):
            while open_headings and open_headings[-1][0] >= heading.level:
                open_headings.pop()
            open_headings.append((heading.level, section_title(heading.title)))
            end = starts[position + 1] if position + 1 < len(starts) else len(text)
            sections.append((heading.start, end, tuple(title for _, title in open_headings)))
        for start, end, section_path in sections:
            for chunk_start, chunk_end in _pack(_pieces(text, start, end, 0, _PARAGRAPH)):
                chunk_text = text[chunk_start:chunk_end]
                chunks.append(
                    Chunk(
                        index=len(chunks),
                        page_start=page.number,
                        page_end=page.number,
                        section_path=section_path,
                        char_start=chunk_start,
                        char_end=chunk_end,
                        text=chunk_text,
                        token_count=count_tokens(chunk_text),
                    )
                )
    return tuple(chunks)
