"""Plain text and Markdown: decode the bytes, then find Markdown's headings.

Decoding follows the validator's rule (``vault.ingest.validate``) and then goes further, because
validation read only the head of the file:

- A byte order mark names the encoding (UTF-8, UTF-16 or UTF-32), and the whole file must be valid
  in it. The mark itself is not part of the text.
- Without a mark the file is UTF-8 if it is valid UTF-8 from end to end, and otherwise Windows-1252,
  the code page Windows writes for Western languages (Notepad's "ANSI" before Windows 10). That is
  a fixed choice, not a guess per file: the result records which one was used, and a file in
  another code page (Cyrillic, Greek, East Asian) decodes to the wrong letters until a later
  version adds detection. A file that mixes UTF-8 with another encoding falls back to Windows-1252
  as a whole.
- The decoded text must pass the validator's text rule (``reads_as_text``) as a whole: no NUL and
  only a few control characters. A file that turns binary after its head is refused here.
- Line breaks are stored as ``\\n`` (Windows ``\\r\\n`` and old Mac ``\\r`` are converted), so
  offsets do not depend on the editor that wrote the file. Nothing else is changed.

A text file is one page. Markdown headings are found by the CommonMark rules for headings at the
top level: ``#`` to ``######`` at the start of a line (up to three spaces in), and a paragraph
underlined with ``===`` (level 1) or ``---`` (level 2). Lines inside fenced code (three or more
backticks or tildes, closed by a fence of the same character at least as long, or open to the
end), indented code and a YAML front matter block at the very start are not headings. Headings
inside block quotes and list items are not recorded either: they are not section headings of the
document. HTML blocks are not recognised, so a ``#`` line inside one counts as a heading; that is
rare in notes and only adds a section break.
"""

from __future__ import annotations

import codecs
import re
from typing import Final

from vault.ingest.extractors import Extraction, Heading, NotTextError, Page
from vault.ingest.validate import TEXT_BOMS, reads_as_text

VERSION: Final = "text-1"
FALLBACK_ENCODING: Final = "cp1252"

_LINE_BREAK: Final = re.compile(r"\r\n?")
_ATX: Final = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*")
_ATX_CLOSING: Final = re.compile(r"[ \t]+#+$")
_SETEXT: Final = re.compile(r" {0,3}(=+|-+)[ \t]*")
_THEMATIC_BREAK: Final = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
_FENCE_OPEN: Final = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_QUOTE: Final = re.compile(r" {0,3}>")
# A list item's marker, then the spaces after it and the first character of its content (none for
# an empty item). Groups: bullet, number, spaces, first character. The marker must be followed by a
# space or the end of the line, which ``_list_item`` checks.
_LIST_ITEM: Final = re.compile(r" {0,3}(?:([-*+])|([0-9]{1,9})[.)])([ \t]*)(\S?)")
_FRONT_MATTER_OPEN: Final = "---"
_FRONT_MATTER_CLOSE: Final = ("---", "...")


def decode(data: bytes) -> tuple[str, str]:
    """The text in ``data`` with ``\\n`` line breaks, and the encoding it was decoded with.

    Raises ``NotTextError`` if the bytes are not valid in the encoding their mark names, or if the
    decoded text is not text by the validator's rule.
    """
    for bom, encoding in TEXT_BOMS:
        if data.startswith(bom):
            text = _strict(data[len(bom) :], encoding)
            break
    else:
        try:
            encoding = "utf-8"
            text = data.decode(encoding)
        except UnicodeDecodeError:
            encoding = FALLBACK_ENCODING
            text = _strict(data, encoding)
    if text is None or not reads_as_text(text):
        raise NotTextError
    return _LINE_BREAK.sub("\n", text), codecs.lookup(encoding).name


def _strict(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


def extract(data: bytes, *, markdown: bool) -> Extraction:
    """One page holding the decoded text, with its headings when ``markdown`` is true."""
    text, encoding = decode(data)
    headings = markdown_headings(text) if markdown else ()
    return Extraction(VERSION, (Page(1, text, headings),), encoding)


def markdown_headings(text: str) -> tuple[Heading, ...]:
    """The top-level headings of Markdown ``text`` (``\\n`` line breaks), in order."""
    lines = text.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    def end_of(index: int) -> int:
        return starts[index] + len(lines[index])

    found: list[Heading] = []
    fence: tuple[str, int] | None = None
    paragraph: int | None = None  # index of the first line of the open top-level paragraph
    item_indent: int | None = None  # content column of the open list item
    lazy = False  # the previous line belongs to a quote or a list item
    index = _after_front_matter(lines)
    while index < len(lines):
        line = lines[index]
        index += 1
        if fence is not None:
            if _closes(line, fence):
                fence = None
            continue
        if not line.strip():
            paragraph = None
            lazy = False
            continue
        if item_indent is not None and _indent(line) >= item_indent:
            continue  # inside the list item
        item_indent = None
        if paragraph is not None:
            setext = _SETEXT.fullmatch(line)
            if setext:
                title = " ".join(part.strip() for part in lines[paragraph : index - 1])
                level = 1 if setext.group(1)[0] == "=" else 2
                found.append(Heading(level, title, starts[paragraph], end_of(index - 1)))
                paragraph = None
                continue
        if _THEMATIC_BREAK.fullmatch(line):
            paragraph, lazy = None, False
            continue
        opened = _FENCE_OPEN.fullmatch(line)
        if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            fence = (opened.group(1)[0], len(opened.group(1)))
            paragraph, lazy = None, False
            continue
        atx = _ATX.fullmatch(line)
        if atx:
            content = atx.group(2) or ""
            title = "" if not content.strip("# \t") else _ATX_CLOSING.sub("", content).strip()
            found.append(Heading(len(atx.group(1)), title, starts[index - 1], end_of(index - 1)))
            paragraph, lazy = None, False
            continue
        if _QUOTE.match(line):
            paragraph, lazy = None, True
            continue
        item = _list_item(line)
        if item and (paragraph is None or _interrupts(item)):
            item_indent = _content_column(line, item)
            paragraph, lazy = None, True
            continue
        if lazy or paragraph is not None:
            continue  # a continuation line of a quote, a list item or the open paragraph
        if _indent(line) >= 4:
            continue  # indented code
        paragraph = index - 1
    return tuple(found)


def _list_item(line: str) -> re.Match[str] | None:
    item = _LIST_ITEM.match(line)
    if item is None or (item.group(4) and not item.group(3)):
        return None  # "-x" or "1.x" is text, not a list item
    return item


def _interrupts(item: re.Match[str]) -> bool:
    """Can this list item start while a paragraph is open? (CommonMark: not empty, and a numbered
    list only from 1, so a line such as "1986. A good year" stays in the paragraph.)"""
    if not item.group(4):
        return False
    number = item.group(2)
    return number is None or int(number) == 1


def _content_column(line: str, item: re.Match[str]) -> int:
    """The column a list item's content starts at: lines indented this far belong to the item.

    One to four spaces after the marker count; with none (an empty item) or more than four, the
    content starts one column after the marker (CommonMark, list items rule 2).
    """
    marker_end = _column(line[: item.start(3)])
    spaces = _column(line[: item.end(3)]) - marker_end
    if not item.group(4) or spaces > 4:
        return marker_end + 1
    return marker_end + spaces


def _closes(line: str, fence: tuple[str, int]) -> bool:
    char, length = fence
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return False
    run = len(stripped) - len(stripped.lstrip(char))
    return run >= length and not stripped[run:].strip(" \t")


def _indent(line: str) -> int:
    """The width of the spaces and tabs ``line`` starts with."""
    return _column(line[: len(line) - len(line.lstrip(" \t"))])


def _column(prefix: str) -> int:
    """The column after ``prefix``, with tab stops every four columns (CommonMark)."""
    width = 0
    for char in prefix:
        width += 4 - width % 4 if char == "\t" else 1
    return width


def _after_front_matter(lines: list[str]) -> int:
    """The index of the first line after a YAML front matter block, or 0 if there is none."""
    if not lines or lines[0].rstrip() != _FRONT_MATTER_OPEN:
        return 0
    for index in range(1, len(lines)):
        if lines[index].rstrip() in _FRONT_MATTER_CLOSE:
            return index + 1
    return 0
