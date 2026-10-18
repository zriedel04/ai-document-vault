"""DOCX: the body's paragraphs and tables in document order, with headings from the built-in styles.

A DOCX is a zip file, and python-docx opens it with ``zipfile``, so the blob is checked before the
library sees it:

1. The validator's DOCX rule runs on the bytes (``validate.check_docx``): size, entry count and
   directory size from the end records before ``zipfile`` opens anything, then encrypted entries,
   member names, compression method, names used twice, the total size and the compression ratio.
   An OLE compound file (a legacy or password-protected Office file) is refused there too.
2. Those sizes are the archive's own claims, and ``zipfile`` reads a member whole in one call
   (``ZipFile.read``), decompressing as much as the deflate stream holds before cutting it to the
   claimed size. A member whose stream is far longer than its claim would fill memory first. So
   every member is read here in small pieces, never past its claimed size, and copied into a new,
   uncompressed zip in memory. python-docx opens only that copy, which holds exactly the bytes the
   validator bounded.

What the text holds:

- Paragraphs and tables of the main body, in order, including those inside content controls.
  Paragraphs are separated by a blank line (``\\n\\n``), like Markdown, and a line break inside a
  paragraph is ``\\n``, so the chunker can tell the two apart. Empty paragraphs are left out.
- A table is one block: one line per row, cells separated by a tab. Inside a cell, paragraphs,
  line breaks and tabs become single spaces so that a row stays one line, and a nested table is
  flattened into its cell the same way. A row whose cells are all empty, and a row deleted as a
  tracked change, are left out.
- Tracked changes are read as if accepted: inserted and moved-in text is kept, deleted and
  moved-out text is left out. A deleted paragraph mark still ends its paragraph.
- Text boxes are kept: each box's paragraphs follow the paragraph that anchors the box. Word writes
  a box twice (a DrawingML shape and a VML copy for old readers, in ``mc:AlternateContent``), so
  only the first ``mc:Choice`` is read and the ``mc:Fallback`` copy is not.
- Field results are kept and field codes are not (``PAGE``, ``HYPERLINK "..."``); hyperlink text is
  kept.
- Hidden text (``w:vanish`` set on the run) is left out. Text hidden through a style is not
  detected and is kept.
- Left out: headers and footers (letterhead and page numbers repeated on every page), footnotes,
  endnotes and comments (separate parts, not the body), equations (Office Math has no ``w:t``) and
  the document's properties (title, author, dates: the metadata stage reads those later).

Headings are paragraphs in the body (not in a table or a text box) whose paragraph style is one of
the built-in ``Heading 1`` to ``Heading 9`` (levels 1 to 9) or ``Title`` (level 1), matched by the
style's name, which Word writes in English whatever the language of its interface. A heading spans
its paragraph's text, so ``text[start:end]`` is the heading as stored, and its title is that text
with white space collapsed. A style that only looks like a heading, or is based on one, is not one.

A Strict Open XML document (Word's "Strict Open XML Document" format, other namespaces) is refused
rather than read as empty.

DOCX has no reliable pages (Word lays them out when it renders), so the whole text is one page, as
for text files. The section anchors in the text view come from the headings.

Errors are ``NotDocxError`` with fixed text naming a reason, never the file's name or content, and
without the library's exception attached, because a library message can quote a part of the file.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from collections.abc import Iterator
from typing import Final

from docx import Document
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement

from vault.ingest.extractors import Extraction, ExtractionError, Heading, Page
from vault.ingest.validate import DEFAULT_LIMITS, Limits, Reason, check_docx

VERSION: Final = "docx-1"
READ_CHUNK: Final = 64 * 1024
PARAGRAPH_BREAK: Final = "\n\n"
CELL_SEPARATOR: Final = "\t"
ROW_SEPARATOR: Final = "\n"

_MC: Final = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_DOCUMENT, _BODY = qn("w:document"), qn("w:body")
_P, _TBL, _TR, _TC, _R = qn("w:p"), qn("w:tbl"), qn("w:tr"), qn("w:tc"), qn("w:r")
_SDT, _SDT_CONTENT, _CUSTOM_XML = qn("w:sdt"), qn("w:sdtContent"), qn("w:customXml")
_DEL, _MOVE_FROM = qn("w:del"), qn("w:moveFrom")
_T, _TAB, _PTAB, _BR, _CR = qn("w:t"), qn("w:tab"), qn("w:ptab"), qn("w:br"), qn("w:cr")
_NO_BREAK_HYPHEN, _TXBX_CONTENT = qn("w:noBreakHyphen"), qn("w:txbxContent")
_RPR, _VANISH, _VAL = qn("w:rPr"), qn("w:vanish"), qn("w:val")
_PPR, _PSTYLE, _TRPR = qn("w:pPr"), qn("w:pStyle"), qn("w:trPr")
_STYLE, _STYLE_ID, _NAME, _TYPE = qn("w:style"), qn("w:styleId"), qn("w:name"), qn("w:type")
_ALTERNATE, _CHOICE = f"{_MC}AlternateContent", f"{_MC}Choice"
# Inline content whose text is not part of the document as accepted: deleted and moved-out runs.
_NOT_TEXT: Final = frozenset({_DEL, _MOVE_FROM})
_RUN_TEXT: Final = {_T: None, _TAB: "\t", _PTAB: "\t", _BR: "\n", _CR: "\n", _NO_BREAK_HYPHEN: "-"}
_OFF: Final = frozenset({"0", "false", "off"})
_HEADING_LEVELS: Final = {"title": 1, **{f"heading {n}": n for n in range(1, 10)}}
_LINE_BREAK: Final = re.compile(r"\r\n?")
_CELL_SPACE: Final = re.compile(r"[\t\n]+")


class NotDocxError(ExtractionError):
    """The blob is not a DOCX this extractor can read. ``reason`` is the validator's reason when its
    DOCX rule refused the bytes, and None when python-docx could not read a part."""

    def __init__(self, reason: Reason | None = None) -> None:
        self.reason = reason
        detail = reason.value if reason is not None else "unreadable_part"
        super().__init__(f"The file is not a readable DOCX ({detail}).")


def extract(data: bytes, limits: Limits = DEFAULT_LIMITS) -> Extraction:
    """One page holding the text of the DOCX in ``data``, with its headings."""
    reason = check_docx(data, limits)
    if reason is not None:
        raise NotDocxError(reason)
    bounded = _bounded_copy(data)
    try:
        document = Document(io.BytesIO(bounded))
    except MemoryError:
        raise
    except Exception:
        # Any failure of the library on these bytes is a document it cannot read; its message may
        # quote the file, so it is not kept. The library itself was imported with this module.
        raise NotDocxError from None
    del bounded
    # A Strict Open XML document (or any other namespace) passes the content type check but has no
    # element this module knows: refuse it instead of returning an empty text.
    root = document.element
    body = root.find(_BODY) if root.tag == _DOCUMENT else None
    if body is None:
        raise NotDocxError
    levels = _heading_styles(document.styles.element)
    parts: list[str] = []
    headings: list[Heading] = []
    position = 0
    for text, level in _segments(body, levels, top=True):
        if not text.strip():
            continue
        if parts:
            position += len(PARAGRAPH_BREAK)
        if level is not None:
            headings.append(Heading(level, " ".join(text.split()), position, position + len(text)))
        parts.append(text)
        position += len(text)
    return Extraction(VERSION, (Page(1, PARAGRAPH_BREAK.join(parts), tuple(headings)),))


def _bounded_copy(data: bytes) -> bytes:
    """An uncompressed zip with every member of ``data``, each read no further than its claimed
    size. ``data`` has passed ``check_docx``, so the claims are within the limits."""
    out = io.BytesIO()
    try:
        with (
            zipfile.ZipFile(io.BytesIO(data)) as archive,
            zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as copy,
        ):
            for info in archive.infolist():
                if info.is_dir():
                    continue
                copy.writestr(info.filename, _read_member(archive, info))
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, NotImplementedError):
        raise NotDocxError(Reason.UNREADABLE_ARCHIVE) from None
    return out.getvalue()


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """The member's bytes, read in pieces of ``READ_CHUNK``. ``zipfile`` stops at the claimed size
    and checks the CRC there; the count below holds the bound even if it did not."""
    pieces: list[bytes] = []
    total = 0
    with archive.open(info) as member:
        while piece := member.read(READ_CHUNK):
            total += len(piece)
            if total > info.file_size:
                raise NotDocxError(Reason.UNSAFE_ARCHIVE)
            pieces.append(piece)
    return b"".join(pieces)


def _heading_styles(styles: BaseOxmlElement) -> dict[str, int]:
    """Paragraph style id to heading level, for the built-in heading and title styles."""
    levels: dict[str, int] = {}
    for style in styles.iterchildren(_STYLE):
        name = style.find(_NAME)
        if style.get(_TYPE) != "paragraph" or name is None:
            continue
        level = _HEADING_LEVELS.get((name.get(_VAL) or "").strip().lower())
        style_id = style.get(_STYLE_ID)
        if level is not None and style_id is not None:
            levels[style_id] = level
    return levels


def _segments(
    container: BaseOxmlElement, levels: dict[str, int], *, top: bool
) -> Iterator[tuple[str, int | None]]:
    """Each paragraph and table under ``container`` as (text, heading level or None), in order.
    Headings are recognised only at the top of the body (``top``)."""
    for block in _blocks(container, (_P, _TBL)):
        if block.tag == _TBL:
            yield _table_text(block, levels), None
            continue
        boxes: list[BaseOxmlElement] = []
        text = _LINE_BREAK.sub("\n", "".join(_inline_text(block, boxes)))
        yield text, _heading_level(block, levels) if top else None
        for box in boxes:
            yield from _segments(box, levels, top=False)


def _blocks(container: BaseOxmlElement, wanted: tuple[str, ...]) -> Iterator[BaseOxmlElement]:
    """The children of ``container`` with a ``wanted`` tag, looking through content controls and
    custom XML wrappers."""
    for child in container.iterchildren():
        if child.tag in wanted:
            yield child
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                yield from _blocks(content, wanted)
        elif child.tag == _CUSTOM_XML:
            yield from _blocks(child, wanted)


def _table_text(table: BaseOxmlElement, levels: dict[str, int]) -> str:
    rows: list[str] = []
    for row in _blocks(table, (_TR,)):
        properties = row.find(_TRPR)
        if properties is not None and properties.find(_DEL) is not None:
            continue
        cells = [
            _CELL_SPACE.sub(
                " ", " ".join(t for t, _ in _segments(cell, levels, top=False) if t.strip())
            ).strip()
            for cell in _blocks(row, (_TC,))
        ]
        if any(cells):
            rows.append(CELL_SEPARATOR.join(cells))
    return ROW_SEPARATOR.join(rows)


def _inline_text(element: BaseOxmlElement, boxes: list[BaseOxmlElement]) -> Iterator[str]:
    """The text of the runs under ``element`` as accepted; text boxes found are added to ``boxes``.
    Recursion is bounded: libxml2 refuses a document nested deeper than 256 elements."""
    for child in element.iterchildren():
        tag = child.tag
        if tag == _R:
            yield from _run_text(child, boxes)
        elif tag == _ALTERNATE:
            choice = child.find(_CHOICE)
            if choice is not None:
                yield from _inline_text(choice, boxes)
        elif tag not in _NOT_TEXT and tag != _PPR:
            yield from _inline_text(child, boxes)  # insertions, hyperlinks, fields, controls


def _run_text(run: BaseOxmlElement, boxes: list[BaseOxmlElement]) -> Iterator[str]:
    properties = run.find(_RPR)
    if properties is not None:
        vanish = properties.find(_VANISH)
        if vanish is not None and (vanish.get(_VAL) or "true").lower() not in _OFF:
            return
    for child in run.iterchildren():
        if child.tag in _RUN_TEXT:
            yield _RUN_TEXT[child.tag] or child.text or ""
        elif child.tag != _RPR:
            boxes.extend(_text_boxes(child))


def _text_boxes(element: BaseOxmlElement) -> Iterator[BaseOxmlElement]:
    """The outermost text box contents under ``element`` (a drawing, a VML picture or an alternate
    block), reading only the first choice of an alternate block."""
    if element.tag == _TXBX_CONTENT:
        yield element
        return
    if element.tag == _ALTERNATE:
        choice = element.find(_CHOICE)
        if choice is not None:
            yield from _text_boxes(choice)
        return
    for child in element.iterchildren():
        yield from _text_boxes(child)


def _heading_level(paragraph: BaseOxmlElement, levels: dict[str, int]) -> int | None:
    properties = paragraph.find(_PPR)
    style = properties.find(_PSTYLE) if properties is not None else None
    if style is None:
        return None
    return levels.get(style.get(_VAL) or "")
