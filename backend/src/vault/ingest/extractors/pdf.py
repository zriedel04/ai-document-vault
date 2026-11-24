"""PDF: the text layer of each page, and the document's own properties, read with pypdfium2
(PDFium).

The library is the one ADR 0012 (Accepted) names for PDF text. Whether it extracts well enough on
real documents is ADR 0015, which is still Proposed until the owner's PDF spike has run, so this
module is the first extractor on that proposal, not a settled choice: if the spike sends the owner
another way, this extractor gets a new version or a replacement. Headings come from the document's
outline (its bookmarks), read through PDFium, and not from font sizes (pdfplumber's, in the
proposal): the public PDF spike found font-size headings with a recall of 0.15 and recommended the
outline instead (see "Headings" below).

What the text holds:

- One page per PDF page, numbered from 1 in the document's order, each with the text PDFium's text
  page gives for it (``FPDFText_GetText``, the method the PDF spike measures), in PDFium's reading
  order. Text outside the page's boxes, annotations and form fields are not read.
- Line breaks are stored as ``\\n`` (PDFium writes ``\\r\\n``). A word that PDFium found hyphenated
  at the end of a line comes back joined: PDFium marks that hyphen with U+FFFE and drops the line
  break after it, and the mark is removed, so ``exam-`` and ``ple`` read ``example``.
- Characters that are not text are removed: control characters other than tab and line feed (a
  broken font map can give them, and NUL cannot be stored), and the noncharacters U+FFFE and U+FFFF.
  A UTF-16 unit that is half of a pair is replaced by U+FFFD, so the text is always storable.
- A page with no text layer, or whose text is only white space, is stored with empty text.
- Each page also carries its measurements for the OCR decision (``Page.layer``, a ``PageLayer``):
  its width and height in points (``FPDF_GetPageWidthF`` and ``HeightF``; the rule uses only
  their product, the area, which a rotation does not change; each taken between 3 and 14,400
  points, ``text_layer.side``), and how many of its glyphs PDFium flags as having no Unicode
  mapping (``FPDFText_HasUnicodeMapError``) while their character reads as a letter, number or
  mark (``text_layer.is_readable``), at most the readable characters of the stored text. The
  extraction stage decides from them and the text whether the page needs OCR
  (``vault.ingest.text_layer``, plan section 7: "a PDF page is OCRed only if its text layer is
  empty or obviously garbage"); nothing here runs OCR.

Headings, from the outline (ISO 32000-1, 12.3.3), read with PDFium's bookmark calls
(``FPDFBookmark_GetFirstChild``, ``GetNextSibling``, ``GetTitle``, ``GetDest``, ``GetAction``):

- Each bookmark is a heading at the level of its depth in the outline (a top-level bookmark is
  level 1), on the page its destination names. A destination is the bookmark's ``Dest`` or the
  destination of its ``GoTo`` action, as PDFium resolves it (a named destination included); a
  bookmark with any other action (a link to another file, a web address, a script) or none, or
  whose destination names no page of this document, is not a heading. Bookmarks deeper than
  ``MAX_OUTLINE_DEPTH`` (9, the deepest level a page may store) are not read, and their text
  falls in the section of their level-9 ancestor.
- The title is the bookmark's own text, decoded from UTF-16 (a lone surrogate becomes U+FFFD),
  with control characters and the noncharacters U+FFFE and U+FFFF turned into spaces and white
  space collapsed to single spaces and trimmed. A bookmark whose title is blank after that, or
  longer than ``MAX_TITLE_UNITS`` UTF-16 units (asked before it is copied out of PDFium), is not
  a heading (left out, not cut); its children still are, at their own levels.
- A bookmark names a page, not a place on it, so each heading is placed on a line of its page's
  text (``place``). For each page, its bookmarks are taken in outline order. First, each is
  matched to the first line after the previous match whose text, with white space collapsed and
  case folded, is its title, or is its title once a section number in front of one of the two is
  removed (``1.2 Results`` on the page and ``Results`` in the outline, as LaTeX writes them; a
  number is digits with dots and an optional ``.``, ``)`` or ``:``, or one letter or a Roman
  numeral with one of those marks, then a space). Then every bookmark gets a line: a matched one
  its match, and one that matched nothing the line after the heading placed before it on the
  page (the top of the page for the first), if that line comes before the next matched heading;
  if none is left, the bookmark is not a heading. A heading spans its line without the white
  space around it. So the section a page-level destination opens starts at the top of the page
  when the title is not found, and the order of the headings on a page follows the outline. The
  destination's position on the page (an ``XYZ`` top, say) is not used.
- The pages are walked in order, so bookmarks whose pages are out of order in the outline become
  headings in page order, and the chunker's section path follows the pages
  (``vault.ingest.chunking.structural_v1``). A page with no text (a scan) has no line for a
  heading, so its bookmarks are not headings until OCR gives it text.
- An outline that cannot be read whole gives no headings at all rather than some of them, and the
  text is read as for a PDF with no outline: one where a bookmark is reached twice (a loop, or a
  bookmark under two parents), one with more than ``MAX_BOOKMARKS`` bookmarks read, or one whose
  headings' titles hold more than ``MAX_TITLE_CHARS`` characters in all. The walk keeps its own
  stack, so a deep or looping outline neither recurses nor runs without end. The caps keep the
  headings of the largest answer inside the sandbox's caps beside the largest text, which a test
  checks.

Bookmark titles are the document's own words: nothing here logs them, and pypdfium2's own outline
walker (``PdfDocument.get_toc``), which logs a warning on a loop or a deep outline, is not used.

Refused, each with an error class of its own and fixed text, never the file's name or content, and
without the library's exception attached (its message is not kept):

- ``EncryptedPdfError``: the PDF needs a password to open, or uses a security handler PDFium does
  not support. A password is never asked for or tried. A PDF encrypted with an owner password only
  (it opens without a password and only restricts printing or copying) is read like any other.
- ``EmptyPdfError``: the PDF has no pages (a version needs at least one page).
- ``PdfLimitError``: more pages than ``Limits.max_pdf_pages``, or more characters of text in all
  than ``Limits.max_pdf_text_chars``. Each page's character count is checked before its text is
  read, and the text read is counted too, because PDFium may insert characters (line breaks,
  spaces) that its count does not hold. The caps keep the answer of a sandboxed extraction inside
  the sandbox's default caps on its size, decoding cost and value count in the worst case, and a
  test checks them against those caps. A PDF over a cap is refused, never cut short.
- ``NotPdfError``: bytes larger than ``Limits.max_bytes``, bytes that do not start like a PDF (the
  validator's header rule), and a file PDFium cannot parse, or a page it cannot load (a page tree
  that claims more pages than it holds, say): the file is refused rather than stored without that
  page.

``read_properties`` reads the document information dictionary (the trailer's ``Info``) for the
metadata stage (``vault.ingest.metadata_stage``): ``Title``, ``Author``, ``CreationDate`` and
``ModDate``, through ``FPDF_GetMetaText``. XMP metadata (the catalog's ``Metadata`` stream) is not
read, so a file that states its properties only there has none. The bytes are checked and opened
exactly as ``extract`` opens them, so a PDF that needs a password, or that PDFium cannot parse,
raises the same error (``EncryptedPdfError``, ``NotPdfError``); its pages are not loaded, so a PDF
with no pages, or a page PDFium cannot load, still has its properties read (extraction refuses
it). What PDFium does with each entry, which the tests pin:

- A text string is decoded by its form (ISO 32000-1, 7.9.2.2): UTF-16BE after the byte order
  mark ``FE FF``, otherwise PDFDocEncoding (Annex D), with the codes that encoding leaves
  undefined given as U+0000. PDFium also takes two marks the standard does not have in PDF 1.7:
  UTF-8 after ``EF BB BF`` (PDF 2.0) and UTF-16LE after ``FF FE``. A UTF-16 unit that is half of
  a pair is replaced by U+FFFD here, so the text is always storable.
- A name is read as its text (``/Foo#20Bar`` reads ``Foo Bar``); a number, a boolean, null, an
  array, a dictionary and a reference to an object that does not exist give no text; an entry
  written twice gives the last one; a missing or empty entry, an ``Info`` that is not a
  dictionary, and a file with no ``Info``, give no text. Each of these
  comes back as None, never as an empty string.
- A value longer than ``MAX_PROPERTY_UNITS`` UTF-16 units is None (left out, not cut), so the
  four values always fit the sandbox's caps on an answer; the stage keeps at most 255 characters
  of a name anyway.

The value is returned as PDFium gives it: nothing is cleaned and no date is parsed, because the
answer comes from the sandbox, which a hostile file may control, so the metadata stage judges
every value itself.

Time and memory are bounded by the process that runs the extractor: the sandbox (``vault.jobs.
sandbox``) with its timeout and memory cap. PDFium is not thread safe, so one lock serialises every
call into it within a process; the sandbox runs each extraction in a process of its own anyway.
"""

from __future__ import annotations

import bisect
import ctypes
import re
import sys
import threading
from collections.abc import Sequence
from typing import Final

import pypdfium2
import pypdfium2.raw as pdfium_c

from vault.ingest import text_layer
from vault.ingest.extractors import (
    Extraction,
    ExtractionError,
    Heading,
    Page,
    PageLayer,
    PdfProperties,
)
from vault.ingest.validate import DEFAULT_LIMITS, PDF_HEADER, Limits, Reason

# pdf-2 added the measurements of each page's text layer (``Page.layer``); pdf-1 had none. pdf-3
# adds the headings from the outline.
VERSION: Final = "pdf-3"

# PDFium's mark for a hyphen at a line end that joins the two halves of a word.
SOFT_HYPHEN_MARK: Final = "￾"
_LINE_BREAK: Final = re.compile(r"\r\n?")
# Control characters but tab and line feed, and the two noncharacters PDFium can give: U+FFFE is
# its mark for a hyphen at a line end that joins the two halves of a word, so removing it joins
# the word.
_NOT_TEXT: Final = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f￾￿]")
_PASSWORD_ERRORS: Final = frozenset({pdfium_c.FPDF_ERR_PASSWORD, pdfium_c.FPDF_ERR_SECURITY})
_LOCK: Final = threading.Lock()
# The entries of the document information dictionary read, by the field of ``PdfProperties`` each
# fills.
PROPERTIES: Final = {
    "title": "Title",
    "author": "Author",
    "created": "CreationDate",
    "modified": "ModDate",
}
# The longest property value returned, in UTF-16 units; a longer one is None.
MAX_PROPERTY_UNITS: Final = 65_536
# The outline's caps (see the module): the deepest level read (the deepest a page may store,
# ``vault.db.pages.HEADING_LEVELS``, which a test ties to it), the bookmarks read in all, the
# longest title taken in UTF-16 units, and the characters of all the headings' titles.
MAX_OUTLINE_DEPTH: Final = 9
MAX_BOOKMARKS: Final = 10_000
MAX_TITLE_UNITS: Final = 1_000
MAX_TITLE_CHARS: Final = 300_000
# What a title holds as a space: control characters and the two noncharacters PDFium can give.
_TITLE_NOT_TEXT: Final = re.compile("[\x00-\x1f\x7f-\x9f\ufffe\uffff]")
_LINE: Final = re.compile(r"[^\n]+")
# A section number in front of a heading, as text or a title may carry it and the other not
# (LaTeX writes "1.2 Results" on the page and "Results" in the outline), case folded: digits with
# dots ("1", "1.2", "1.2."), or one letter or a Roman numeral with its mark ("a)", "iv."), so that
# a word such as "did" or "a" is not taken for a number.
_SECTION_NUMBER: Final = re.compile(r"[0-9]+(?:\.[0-9]+)*[.):]?|(?:[a-z]|[ivxlcdm]+)[.):]")


class NotPdfError(ExtractionError):
    """The blob is not a PDF this extractor can read. ``reason`` is the validator's reason when the
    bytes were refused before PDFium saw them, and None when PDFium could not read the file or one
    of its pages."""

    def __init__(self, reason: Reason | None = None) -> None:
        self.reason = reason
        detail = reason.value if reason is not None else "unreadable"
        super().__init__(f"The file is not a readable PDF ({detail}).")


class EncryptedPdfError(ExtractionError):
    """The PDF cannot be opened without a password (or uses an unsupported security handler).
    The extractor never asks for or tries a password."""

    def __init__(self) -> None:
        super().__init__("The PDF is encrypted and needs a password, which is never asked for.")


class EmptyPdfError(ExtractionError):
    """The PDF opens but has no pages, and a version has at least one page."""

    def __init__(self) -> None:
        super().__init__("The PDF has no pages.")


class PdfLimitError(ExtractionError):
    """The PDF has more pages, or more text, than the limits allow. ``limit`` is ``"pages"`` or
    ``"text"``."""

    def __init__(self, limit: str) -> None:
        self.limit = limit
        super().__init__(f"The PDF is over the {limit} limit.")


def extract(data: bytes, limits: Limits = DEFAULT_LIMITS) -> Extraction:
    """One page for each page of the PDF in ``data``, with the text of its text layer."""
    _check(data, limits)
    with _LOCK:
        return Extraction(VERSION, _pages(data, limits))


def read_properties(data: bytes, limits: Limits = DEFAULT_LIMITS) -> PdfProperties:
    """The document information dictionary of the PDF in ``data``, each value as PDFium gives it
    or None (see the module)."""
    _check(data, limits)
    with _LOCK:
        document = _open(data)
        try:
            values = {field: _meta_text(document, key) for field, key in PROPERTIES.items()}
        finally:
            document.close()  # as in ``_pages``: ``data`` is referenced until here
    return PdfProperties(**values)


def _check(data: bytes, limits: Limits) -> None:
    """The validator's rules for a PDF that hold before PDFium sees the bytes."""
    if not data:
        raise NotPdfError(Reason.EMPTY)
    if len(data) > limits.max_bytes:
        raise NotPdfError(Reason.TOO_LARGE)
    if not PDF_HEADER.match(data):
        raise NotPdfError(Reason.CONTENT_MISMATCH)


def _open(data: bytes) -> pypdfium2.PdfDocument:
    """The PDF in ``data`` opened without a password, which the caller closes."""
    # The raw call, not ``PdfDocument(data)``: that wrapper reads PDFium's last error also when
    # the document opened with no pages, and PDFium does not clear it on success, so a file with no
    # pages read after an encrypted one would be reported as encrypted.
    raw = pdfium_c.FPDF_LoadMemDocument64(data, len(data), None)
    if not raw:
        if pdfium_c.FPDF_GetLastError() in _PASSWORD_ERRORS:
            raise EncryptedPdfError
        raise NotPdfError
    # The wrapper owns the document from here and closes it once (``close`` detaches its finalizer).
    return pypdfium2.PdfDocument(raw)


def _meta_text(document: pypdfium2.PdfDocument, key: str) -> str | None:
    """The text PDFium gives for the entry ``key`` of the document information dictionary, or
    None when it gives none or more than ``MAX_PROPERTY_UNITS`` UTF-16 units."""
    tag = key.encode("ascii") + b"\0"
    # The size in bytes of the value in UTF-16LE with its two-byte terminator, asked first so that
    # a value over the bound is never copied out of PDFium.
    size = pdfium_c.FPDF_GetMetaText(document, tag, None, 0)
    if size <= 2 or size % 2 or size - 2 > 2 * MAX_PROPERTY_UNITS:
        return None
    buffer = ctypes.create_string_buffer(size)
    if pdfium_c.FPDF_GetMetaText(document, tag, buffer, size) != size:
        return None
    return buffer.raw[: size - 2].decode("utf-16-le", errors="replace")


def _pages(data: bytes, limits: Limits) -> tuple[Page, ...]:
    document = _open(data)
    try:
        count = len(document)
        if count < 1:
            raise EmptyPdfError
        if count > limits.max_pdf_pages:
            raise PdfLimitError("pages")
        bookmarks = outline(document, count)
        pages: list[Page] = []
        total = 0
        for index in range(count):
            text, layer = _page_text(document, index, limits.max_pdf_text_chars - total)
            total += len(text)
            headings = place(text, bookmarks.get(index, ()))
            pages.append(Page(index + 1, text, headings, layer=layer))
        return tuple(pages)
    finally:
        # ``data`` is referenced until here: PDFium reads the bytes in place while the document is
        # open, so the document is closed before they can go.
        document.close()


def _page_text(document: pypdfium2.PdfDocument, index: int, budget: int) -> tuple[str, PageLayer]:
    """The cleaned text of page ``index``, if it fits in ``budget`` characters, and the
    measurements of its text layer."""
    try:
        page = document.get_page(index)
    except pypdfium2.PdfiumError:
        raise NotPdfError from None
    try:
        width, height = page.get_size()
        try:
            textpage = page.get_textpage()
        except pypdfium2.PdfiumError:
            raise NotPdfError from None
        try:
            count = textpage.count_chars()
            if count > budget:
                raise PdfLimitError("text")
            raw = textpage.get_text_range(errors="replace")
            unmapped = _unmapped(textpage, count)
        finally:
            textpage.close()
    finally:
        page.close()
    text = clean(raw)
    if len(text) > budget:
        raise PdfLimitError("text")
    layer = PageLayer(
        width=text_layer.side(float(width)),
        height=text_layer.side(float(height)),
        # A glyph PDFium counts may not be in the text it returns (it drops some codes), so the
        # count never goes past what the stored text holds: the stage refuses a larger one.
        unmapped=min(unmapped, text_layer.counts(text)[1]),
    )
    return text, layer


def _unmapped(textpage: pypdfium2.PdfTextPage, count: int) -> int:
    """How many of the ``count`` characters PDFium flags as having no Unicode mapping while the
    character it gives (the raw character code) reads as a letter, number or mark."""
    unmapped = 0
    for char_index in range(count):
        if pdfium_c.FPDFText_HasUnicodeMapError(textpage.raw, char_index) != 1:
            continue
        code = pdfium_c.FPDFText_GetUnicode(textpage.raw, char_index)
        if 0 < code <= sys.maxunicode and text_layer.is_readable(chr(code)):
            unmapped += 1
    return unmapped


def clean(raw: str) -> str:
    """PDFium's page text as stored: ``\\n`` line breaks, hyphenated words joined, characters that
    are not text removed, and a page of only white space empty."""
    text = _NOT_TEXT.sub("", _LINE_BREAK.sub("\n", raw))
    return text if text.strip() else ""


class _OutlineUnreadableError(Exception):
    """The outline loops or is over a cap; it gives no headings."""


def outline(document: pypdfium2.PdfDocument, count: int) -> dict[int, tuple[tuple[int, str], ...]]:
    """The headings the outline of ``document`` (with ``count`` pages) gives, as (level, title) in
    outline order, by the index of the page each is on; empty when it has none or cannot be read
    whole (see the module)."""
    try:
        return _read_outline(document, count)
    except _OutlineUnreadableError:
        return {}


def _read_outline(
    document: pypdfium2.PdfDocument, count: int
) -> dict[int, tuple[tuple[int, str], ...]]:
    found: dict[int, list[tuple[int, str]]] = {}
    seen: set[int] = set()
    title_chars = 0
    # A stack of (bookmark, level) still to read, so that the walk never recurses: the last
    # pushed is read first, and a bookmark's next sibling is pushed before its first child.
    stack = [(pdfium_c.FPDFBookmark_GetFirstChild(document.raw, None), 1)]
    while stack:
        bookmark, level = stack.pop()
        if not bookmark:
            continue
        address = ctypes.cast(bookmark, ctypes.c_void_p).value
        if address is None or address in seen:
            raise _OutlineUnreadableError
        seen.add(address)
        if len(seen) > MAX_BOOKMARKS:
            raise _OutlineUnreadableError
        stack.append((pdfium_c.FPDFBookmark_GetNextSibling(document.raw, bookmark), level))
        if level < MAX_OUTLINE_DEPTH:
            stack.append((pdfium_c.FPDFBookmark_GetFirstChild(document.raw, bookmark), level + 1))
        page = _destination_page(document, bookmark, count)
        title = _bookmark_title(bookmark) if page is not None else None
        if page is None or not title:
            continue
        title_chars += len(title)
        if title_chars > MAX_TITLE_CHARS:
            raise _OutlineUnreadableError
        found.setdefault(page, []).append((level, title))
    return {page: tuple(headings) for page, headings in found.items()}


def _destination_page(document: pypdfium2.PdfDocument, bookmark: object, count: int) -> int | None:
    """The index of the page of this document that ``bookmark`` goes to, or None."""
    action = pdfium_c.FPDFBookmark_GetAction(bookmark)
    # PDFium's GetDest also gives the destination of a GoToR action, a page of another file.
    if action and pdfium_c.FPDFAction_GetType(action) != pdfium_c.PDFACTION_GOTO:
        return None
    destination = pdfium_c.FPDFBookmark_GetDest(document.raw, bookmark)
    if not destination:
        return None
    index = pdfium_c.FPDFDest_GetDestPageIndex(document.raw, destination)
    return index if 0 <= index < count else None


def _bookmark_title(bookmark: object) -> str | None:
    """The cleaned title of ``bookmark`` (see the module), or None when it is longer than
    ``MAX_TITLE_UNITS`` UTF-16 units or cannot be read."""
    # The size in bytes of the title in UTF-16LE with its two-byte terminator, asked first so that
    # a title over the bound is never copied out of PDFium.
    size = pdfium_c.FPDFBookmark_GetTitle(bookmark, None, 0)
    if size <= 2 or size % 2 or size - 2 > 2 * MAX_TITLE_UNITS:
        return None
    buffer = ctypes.create_string_buffer(size)
    if pdfium_c.FPDFBookmark_GetTitle(bookmark, buffer, size) != size:
        return None
    return clean_title(buffer.raw[: size - 2].decode("utf-16-le", errors="replace"))


def clean_title(raw: str) -> str:
    """A bookmark's title as a heading holds it: control characters and the noncharacters U+FFFE
    and U+FFFF become spaces, and white space is collapsed to single spaces and trimmed."""
    return " ".join(_TITLE_NOT_TEXT.sub(" ", raw).split())


def place(text: str, bookmarks: Sequence[tuple[int, str]]) -> tuple[Heading, ...]:
    """The headings of a page with ``text`` for its ``bookmarks`` (level, title) in outline order,
    each on a line of the text (see the module).

    Each title is looked up in an index of the lines rather than compared with every line, so a
    page of the largest text with every bookmark the caps allow is placed in time proportional to
    their sum, not their product."""
    if not bookmarks:
        return ()
    lines = [
        (start, end, " ".join(text[start:end].split()).casefold())
        for match in _LINE.finditer(text)
        for start, end in [_stripped(text, match.start(), match.end())]
        if start < end
    ]
    # Line numbers in order by each line's key, and by its key without a section number: the
    # three ways ``_same`` can hold.
    by_key: dict[str, list[int]] = {}
    by_unnumbered: dict[str, list[int]] = {}
    for number, (_start, _end, key) in enumerate(lines):
        by_key.setdefault(key, []).append(number)
        bare = _unnumbered(key)
        if bare is not None:
            by_unnumbered.setdefault(bare, []).append(number)
    matched: list[int | None] = []
    after = 0
    for _level, title in bookmarks:
        key = title.casefold()
        bare = _unnumbered(key)
        candidates = [
            _first_from(by_key.get(key, ()), after),
            _first_from(by_unnumbered.get(key, ()), after),
            _first_from(by_key.get(bare, ()) if bare is not None else (), after),
        ]
        line = min((n for n in candidates if n is not None), default=None)
        matched.append(line)
        if line is not None:
            after = line + 1
    # For each bookmark, the line of the next matched one after it (or the end of the lines).
    following = [len(lines)] * len(bookmarks)
    upcoming = len(lines)
    for position in range(len(bookmarks) - 1, -1, -1):
        following[position] = upcoming
        line = matched[position]
        if line is not None:
            upcoming = line
    headings: list[Heading] = []
    previous = -1
    for position, (level, title) in enumerate(bookmarks):
        line = matched[position]
        if line is None:
            if previous + 1 >= following[position]:
                continue
            line = previous + 1
        start, end, _key = lines[line]
        headings.append(Heading(level, title, start, end))
        previous = line
    return tuple(headings)


def _first_from(numbers: Sequence[int], after: int) -> int | None:
    """The first of the sorted ``numbers`` that is ``after`` or later, or None."""
    position = bisect.bisect_left(numbers, after)
    return numbers[position] if position < len(numbers) else None


def _same(line: str, title: str) -> bool:
    """Whether a line of text and a title, each with white space collapsed and case folded, are
    the same heading: equal, or equal once a section number in front of one of them is removed."""
    return line == title or _unnumbered(line) == title or line == _unnumbered(title)


def _unnumbered(key: str) -> str | None:
    """``key`` without the section number in front of it, or None when it has none."""
    number, space, rest = key.partition(" ")
    if not space or not rest or not _SECTION_NUMBER.fullmatch(number):
        return None
    return rest


def _stripped(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
