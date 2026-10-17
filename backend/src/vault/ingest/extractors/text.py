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

A text file is one page. Markdown headings are found by the CommonMark rules for headings at the
top level: ``#`` to ``######`` at the start of a line (up to three spaces in), and a paragraph
underlined with ``===`` (level 1) or ``---`` (level 2). Lines inside fenced code (three or more
backticks or tildes, closed by a fence of the same character at least as long, or open to the
end), indented code and a YAML front matter block at the very start are not headings. Headings
inside block quotes and list items are not recorded either: they are not section headings of the
document. A line after a quote or list item continues it only when that item holds a paragraph,
as CommonMark's lazy continuation says. Two simplifications remain: HTML blocks are not recognised,
so a ``#`` line inside one counts as a heading, and the lines of a fenced code block inside a list
item are read as paragraph text, so a line right after such a block may be taken as continuing the
item and its underline missed. Both are rare in notes and change only where a section starts.
"""

from __future__ import annotations

import codecs
import re
from typing import Final

from vault.ingest.extractors import Extraction, Heading, NotTextError, Page
from vault.ingest.validate import TEXT_BOMS, reads_as_text

VERSION: Final = "text-1"
FALLBACK_ENCODING: Final = "cp1252"
# The Windows "ANSI" code pages, one of which the owner may choose as the fallback.
FALLBACK_ENCODINGS: Final = frozenset(
    {"cp874", "cp932", "cp936", "cp949", "cp950", *(f"cp{n}" for n in range(1250, 1259))}
)

_LINE_BREAK: Final = re.compile(r"\r\n?")
_ATX: Final = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*")
# The closing sequence: the last run of #, after a space or tab, or making up the whole content.
_ATX_CLOSING: Final = re.compile(r"(?:^|[ \t]+)#+$")
_SETEXT: Final = re.compile(r" {0,3}(=+|-+)[ \t]*")
_THEMATIC_BREAK: Final = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
# A link reference definition (CommonMark 4.7) at the start of the text: a label with something
# other than spaces in it, a destination, an optional title (on the same line or the next; a title
# that does not end its line leaves the definition without one), then the end of a line. Matched
# repeatedly over a paragraph's lines, so a definition's title may continue over lines.
_DEFINITION: Final = re.compile(
    r" {0,3}\[(?=[^\]]*?[^\s\]])(?:[^\\\[\]]|\\.){1,999}\]:"
    r"[ \t]*\n?[ \t]*(?:<(?:[^<>\n\\]|\\.)*>|[^<\s\x00-\x1f][^\s\x00-\x1f]*)"
    r"(?:(?:[ \t]*\n[ \t]*|[ \t]+)"
    r"(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\((?:[^()\\]|\\.)*\)))?"
    r"[ \t]*(?:\n|$)"
)
_FENCE_OPEN: Final = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_QUOTE: Final = re.compile(r" {0,3}>")
# A list item's marker, then the spaces after it and the first character of its content (none for
# an empty item). Groups: bullet, number, spaces, first character. The marker must be followed by a
# space or the end of the line, which ``_list_item`` checks.
_LIST_ITEM: Final = re.compile(r" {0,3}(?:([-*+])|([0-9]{1,9})[.)])([ \t]*)(\S?)")
_FRONT_MATTER_OPEN: Final = "---"
_FRONT_MATTER_CLOSE: Final = ("---", "...")


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
    items: list[int] = []  # content columns of the open list items, outermost first
    item_fence: tuple[str, int] | None = None  # fenced code open inside the list items
    empty_item = False  # the innermost list item opened empty and has no line yet
    lazy = False  # the previous line continues a paragraph inside a quote or a list item
    quoted = False  # that paragraph is in a quote
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
            lazy = quoted = False
            if empty_item:  # an empty item followed by a blank line ends (CommonMark)
                items.pop()
                empty_item = False
                if not items:
                    item_fence = None
            continue
        indent = _indent(line)
        if items and indent >= items[0]:
            # Inside a list item: judge the line by its indent within the innermost item it
            # reaches, not by its column on the page.
            while indent < items[-1]:
                items.pop()
            empty_item = quoted = False
            inner = _dedent(line, items[-1])
            if item_fence is not None:
                if _closes(inner, item_fence):
                    item_fence = None
                continue
            if lazy and _indent(inner) >= 4:
                continue  # indented code cannot interrupt a paragraph: this line continues it
            item_fence = _fence_open(inner)
            if item_fence is not None:
                lazy = False
                continue
            nested = _list_item(inner)
            if nested and (not lazy or _interrupts(nested)):
                items.append(_content_column(inner, nested, items[-1]))
                empty_item = not nested.group(4)
            lazy = _opens_paragraph(inner)
            continue
        if items and lazy and _continues(line):
            continue  # a lazy line of the item's paragraph: the item stays open
        items.clear()
        item_fence = None  # fenced code in a list item ends with the item
        empty_item = False
        if paragraph is not None:
            setext = _SETEXT.fullmatch(line)
            if setext:
                # Link reference definitions at the start of the paragraph are taken out of it
                # when it closes (CommonMark 4.7); the heading is the rest. With nothing left
                # there is no heading: the line is a thematic break if it is one, and otherwise
                # it continues the paragraph, which an underline that follows may still close.
                first = paragraph + _definition_lines(lines[paragraph : index - 1])
                if first < index - 1:
                    title = " ".join(part.strip() for part in lines[first : index - 1])
                    level = 1 if setext.group(1)[0] == "=" else 2
                    found.append(Heading(level, title, starts[first], end_of(index - 1)))
                    paragraph = None
                    continue
                if not _THEMATIC_BREAK.fullmatch(line):
                    continue
                paragraph = None
        if _THEMATIC_BREAK.fullmatch(line):
            paragraph, lazy, quoted = None, False, False
            continue
        opened = _fence_open(line)
        if opened:
            fence = opened
            paragraph, lazy, quoted = None, False, False
            continue
        atx = _ATX.fullmatch(line)
        if atx:
            title = _ATX_CLOSING.sub("", atx.group(2) or "", count=1).strip()
            found.append(Heading(len(atx.group(1)), title, starts[index - 1], end_of(index - 1)))
            paragraph, lazy, quoted = None, False, False
            continue
        if _QUOTE.match(line):
            # Indented code cannot interrupt the quote's open paragraph: such a line continues it.
            lazy = (quoted and lazy and _quoted_indent(line) >= 4) or _opens_paragraph(line)
            paragraph, quoted = None, True
            continue
        item = _list_item(line)
        if item and (paragraph is None or _interrupts(item)):
            items[:] = [_content_column(line, item)]
            empty_item = not item.group(4)
            paragraph, lazy, quoted = None, _opens_paragraph(line), False
            continue
        if lazy or paragraph is not None:
            continue  # a continuation line of a quote, a list item or the open paragraph
        if _indent(line) >= 4:
            continue  # indented code
        paragraph = index - 1
    return tuple(found)


def _opens_paragraph(line: str) -> bool:
    """Does ``line`` hold paragraph text, inside any quotes and list items it opens?

    Only a paragraph takes lazy continuation lines (CommonMark): after ``> # Quoted`` the quote
    ends, so a following ``Outside`` line starts a paragraph of its own and can be underlined.
    """
    if not line.strip() or _indent(line) >= 4:
        return False  # blank, or indented code
    quote = _QUOTE.match(line)
    if quote:
        inner = line[quote.end() :]
        return _opens_paragraph(inner[1:] if inner[:1] in (" ", "\t") else inner)
    item = _list_item(line)
    if item:
        return bool(item.group(4)) and _opens_paragraph(line[item.start(4) :])
    return not (_ATX.fullmatch(line) or _THEMATIC_BREAK.fullmatch(line) or _fence_open(line))


def _definition_lines(lines: list[str]) -> int:
    """How many of a paragraph's ``lines`` the link reference definitions at its start take."""
    if not lines or not lines[0].lstrip(" ").startswith("["):
        return 0
    text = "\n".join(lines)
    position = 0
    while match := _DEFINITION.match(text, position):
        position = match.end()
    return text.count("\n", 0, position) + (position == len(text) and position > 0)


def _quoted_indent(line: str) -> int:
    """The indent of ``line``'s text inside all the quote markers it starts with."""
    while quote := _QUOTE.match(line):
        inner = line[quote.end() :]
        line = inner[1:] if inner[:1] == " " else inner
    return _indent(line)


def _continues(line: str) -> bool:
    """Is ``line``, left of an open list item, a lazy line of the item's paragraph?

    Any list item starts a block here, even one that could not interrupt a paragraph: that rule
    holds only within the same container, and this line is outside the item (CommonMark).
    """
    return not (
        _THEMATIC_BREAK.fullmatch(line)
        or _ATX.fullmatch(line)
        or _fence_open(line)
        or _QUOTE.match(line)
        or _list_item(line)
    )


def _fence_open(line: str) -> tuple[str, int] | None:
    """The fence character and length if ``line`` opens fenced code. A backtick fence whose info
    string holds a backtick is not one (CommonMark): that line is paragraph text."""
    opened = _FENCE_OPEN.fullmatch(line)
    if opened is None or (opened.group(1)[0] == "`" and "`" in opened.group(2)):
        return None
    return opened.group(1)[0], len(opened.group(1))


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


def _content_column(line: str, item: re.Match[str], start: int = 0) -> int:
    """The column a list item's content starts at: lines indented this far belong to the item.
    ``line`` starts at column ``start`` (the content column of the item it is nested in).

    One to four spaces after the marker count; with none (an empty item) or more than four, the
    content starts one column after the marker (CommonMark, list items rule 2).
    """
    marker_end = _column(line[: item.start(3)], start)
    spaces = _column(line[: item.end(3)], start) - marker_end
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


def _column(prefix: str, start: int = 0) -> int:
    """The column after ``prefix`` when it starts at column ``start``, with tab stops every four
    columns (CommonMark)."""
    width = start
    for char in prefix:
        width += 4 - width % 4 if char == "\t" else 1
    return width


def _dedent(line: str, columns: int) -> str:
    """``line`` without its first ``columns`` columns of indentation (it has at least that many),
    with the rest of its indentation as spaces, so a tab is split where the boundary falls."""
    body = line.lstrip(" \t")
    return " " * (_indent(line) - columns) + body


def _after_front_matter(lines: list[str]) -> int:
    """The index of the first line after a YAML front matter block, or 0 if there is none."""
    if not lines or lines[0].rstrip() != _FRONT_MATTER_OPEN:
        return 0
    for index in range(1, len(lines)):
        if lines[index].rstrip() in _FRONT_MATTER_CLOSE:
            return index + 1
    return 0
