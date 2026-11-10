"""PDF: the text layer of each page, read with pypdfium2 (PDFium).

The library is the one ADR 0012 (Accepted) names for PDF text. Whether it extracts well enough on
real documents is ADR 0015, which is still Proposed until the owner's PDF spike has run, so this
module is the first extractor on that proposal, not a settled choice: if the spike sends the owner
another way, this extractor gets a new version or a replacement. Heading detection (pdfplumber's
font sizes, in the proposal) is not done here: a page has no headings for now.

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
- A page with no text layer, or whose text is only white space, is stored with empty text. That is
  the mark for the OCR stage of Phase 3 (plan section 7: "a PDF page is OCRed only if its text layer
  is empty or obviously garbage"): it looks for a PDF page whose ``text`` is ``""``. No new field is
  added for it; the "obviously garbage" half of the rule needs the page area, which arrives with
  that stage.

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

Time and memory are bounded by the process that runs the extractor: the sandbox (``vault.jobs.
sandbox``) with its timeout and memory cap. PDFium is not thread safe, so one lock serialises every
call into it within a process; the sandbox runs each extraction in a process of its own anyway.
"""

from __future__ import annotations

import re
import threading
from typing import Final

import pypdfium2
import pypdfium2.raw as pdfium_c

from vault.ingest.extractors import Extraction, ExtractionError, Page
from vault.ingest.validate import DEFAULT_LIMITS, PDF_HEADER, Limits, Reason

VERSION: Final = "pdf-1"

# PDFium's mark for a hyphen at a line end that joins the two halves of a word.
SOFT_HYPHEN_MARK: Final = "￾"
_LINE_BREAK: Final = re.compile(r"\r\n?")
# Control characters but tab and line feed, and the two noncharacters PDFium can give: U+FFFE is
# its mark for a hyphen at a line end that joins the two halves of a word, so removing it joins
# the word.
_NOT_TEXT: Final = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f￾￿]")
_PASSWORD_ERRORS: Final = frozenset({pdfium_c.FPDF_ERR_PASSWORD, pdfium_c.FPDF_ERR_SECURITY})
_LOCK: Final = threading.Lock()


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
    if not data:
        raise NotPdfError(Reason.EMPTY)
    if len(data) > limits.max_bytes:
        raise NotPdfError(Reason.TOO_LARGE)
    if not PDF_HEADER.match(data):
        raise NotPdfError(Reason.CONTENT_MISMATCH)
    with _LOCK:
        return Extraction(VERSION, _pages(data, limits))


def _pages(data: bytes, limits: Limits) -> tuple[Page, ...]:
    # The raw call, not ``PdfDocument(data)``: that wrapper reads PDFium's last error also when
    # the document opened with no pages, and PDFium does not clear it on success, so a file with no
    # pages read after an encrypted one would be reported as encrypted.
    raw = pdfium_c.FPDF_LoadMemDocument64(data, len(data), None)
    if not raw:
        if pdfium_c.FPDF_GetLastError() in _PASSWORD_ERRORS:
            raise EncryptedPdfError
        raise NotPdfError
    # The wrapper owns the document from here and closes it once (``close`` detaches its finalizer).
    document = pypdfium2.PdfDocument(raw)
    try:
        count = len(document)
        if count < 1:
            raise EmptyPdfError
        if count > limits.max_pdf_pages:
            raise PdfLimitError("pages")
        pages: list[Page] = []
        total = 0
        for index in range(count):
            text = _page_text(document, index, limits.max_pdf_text_chars - total)
            total += len(text)
            pages.append(Page(index + 1, text))
        return tuple(pages)
    finally:
        # ``data`` is referenced until here: PDFium reads the bytes in place while the document is
        # open, so the document is closed before they can go.
        document.close()


def _page_text(document: pypdfium2.PdfDocument, index: int, budget: int) -> str:
    """The cleaned text of page ``index``, if it fits in ``budget`` characters."""
    try:
        page = document.get_page(index)
    except pypdfium2.PdfiumError:
        raise NotPdfError from None
    try:
        try:
            textpage = page.get_textpage()
        except pypdfium2.PdfiumError:
            raise NotPdfError from None
        try:
            if textpage.count_chars() > budget:
                raise PdfLimitError("text")
            raw = textpage.get_text_range(errors="replace")
        finally:
            textpage.close()
    finally:
        page.close()
    text = clean(raw)
    if len(text) > budget:
        raise PdfLimitError("text")
    return text


def clean(raw: str) -> str:
    """PDFium's page text as stored: ``\\n`` line breaks, hyphenated words joined, characters that
    are not text removed, and a page of only white space empty."""
    text = _NOT_TEXT.sub("", _LINE_BREAK.sub("\n", raw))
    return text if text.strip() else ""
