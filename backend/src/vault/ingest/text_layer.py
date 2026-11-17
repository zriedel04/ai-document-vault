"""Whether a PDF page's text layer is usable, or the page needs OCR (plan section 7, step 8).

The plan's rule: "a PDF page is OCRed only if its text layer is empty or obviously garbage (too few
characters per page area, mostly non-letter glyphs)". This module is that decision, made for each
page from the stored text and what the PDF extractor measured of the page (``PageLayer``: its size
and how many of its glyphs had no Unicode mapping). It runs no OCR and loads no OCR engine (ADR 0014
is still Proposed): the OCR stage of Phase 3 finds the pages to read by the stored outcome.

Terms, all counted in the page's stored text (characters, as Python counts them):

- *visible*: a character that is not white space (``str.isspace``);
- *readable*: a visible character of the Unicode categories letter, number or mark (L, N, M: marks
  so that a script written with combining marks, such as Devanagari, is not counted as garbage),
  less the glyphs the extractor counted in ``PageLayer.unmapped``. A replacement character
  (U+FFFD, a symbol), a private-use character (Co), an unassigned code point (Cn) and punctuation
  are visible but not readable.

The rules, applied in this order; the first that holds gives the outcome:

1. ``empty``: no visible character (the extractor stores such a page as ``""``). A scan, a blank
   page, a page of white space.
2. ``unreadable``: fewer than half of the visible characters are readable
   (``2 * readable < visible``; exactly half is usable). "Mostly non-letter glyphs" in the plan's
   words: a font whose glyphs PDFium cannot map to Unicode comes out as private-use characters,
   replacement characters or, with no ``ToUnicode`` map and glyph names it does not know, as the
   raw character codes, which look like letters; PDFium flags each of those
   (``FPDFText_HasUnicodeMapError``) and the extractor counts the ones that would read as letters
   or numbers, so they are not counted as readable. Half, because a real page of text is mostly
   letters and digits even with heavy punctuation (a table of figures, a list of references), and
   a page that is half symbols is not one keyword search can use.
3. ``sparse``: fewer than ``MIN_VISIBLE_PER_A4`` (10) visible characters for each area of an A4
   page (``visible * A4_AREA < MIN_VISIBLE_PER_A4 * width * height``; exactly 10 is usable). "Too
   few characters per page area": the typical case is a scan whose text layer holds only a page
   number, a date or a short stamp. Measured per area so that one line on a poster-sized page counts
   for as little as a page number on A4. Ten, because the cost of a wrong answer is not the same in
   both directions: a sparse page of real text that is OCRed costs time and gives the same words
   again, while a scan judged usable is never read. Ten keeps the shortest real pages (a title page
   of one word on A4 is sparse, a slide whose title is "Questions?" is usable) on the cheap side,
   and leaves a scan with a stamp of ten characters or more judged usable: a known limit of a rule
   that does not look at the page's images, written down here rather than hidden.
4. ``usable``: otherwise. The text layer is kept and the page is not OCRed.

Each side of the page is taken between 3 and 14,400 points (``MIN_SIDE``, ``MAX_SIDE``, the limits
ISO 32000-1 Annex C gives a page), so a malformed box can neither divide by zero nor make every page
sparse; a size PDFium gives as not finite is taken as the smallest, which makes no page sparse.

``unjudged`` is not an outcome of this rule: it marks a page of text the first PDF extractor
(``pdf-1``) stored before the rule existed, without the measurements the rule needs (migration
0008). Such a page is judged only when its version is extracted again with a current extractor,
which nothing does on its own yet: the OCR stage has to re-extract those versions (or OCR them).

Messages are fixed text: they never quote the page.
"""

from __future__ import annotations

import math
import unicodedata
from enum import StrEnum
from typing import Final

from vault.ingest.extractors import PageLayer


class TextLayer(StrEnum):
    """The stored outcome for a PDF page (``pages.text_layer``; ``vault.db.schema.TEXT_LAYERS``
    lists the same values, and a test ties the two)."""

    USABLE = "usable"
    EMPTY = "empty"
    SPARSE = "sparse"
    UNREADABLE = "unreadable"
    UNJUDGED = "unjudged"


# The outcomes that send a page to the OCR stage.
NEEDS_OCR: Final = frozenset({TextLayer.EMPTY, TextLayer.SPARSE, TextLayer.UNREADABLE})

# An A4 page in PDF points (210 x 297 mm at 72 points to the inch, rounded to whole points).
A4_AREA: Final = 595.0 * 842.0
MIN_VISIBLE_PER_A4: Final = 10
MIN_SIDE: Final = 3.0
MAX_SIDE: Final = 14_400.0
_READABLE_CATEGORIES: Final = frozenset("LNM")


def is_readable(character: str) -> bool:
    """True for a letter, a number or a mark (Unicode categories L, N and M)."""
    return unicodedata.category(character)[0] in _READABLE_CATEGORIES


def side(points: float) -> float:
    """A page side as the rule takes it: between ``MIN_SIDE`` and ``MAX_SIDE``, and the smallest
    for a value that is not a finite number."""
    if not math.isfinite(points):
        return MIN_SIDE
    return min(max(points, MIN_SIDE), MAX_SIDE)


def counts(text: str) -> tuple[int, int]:
    """The visible and the readable characters of ``text``, before unmapped glyphs are taken off."""
    visible = readable = 0
    for character in text:
        if not character.isspace():
            visible += 1
            readable += is_readable(character)
    return visible, readable


def check_layer(text: str, layer: object) -> PageLayer:
    """``layer`` if it is a measurement the PDF extractor can give for ``text``: sides between
    ``MIN_SIDE`` and ``MAX_SIDE``, and no more unmapped glyphs than readable characters (the
    extractor counts only glyphs that read as letters, numbers or marks). ``ValueError`` if not."""
    if not isinstance(layer, PageLayer):
        msg = "A PDF page needs the measurements of its text layer."
        raise ValueError(msg)
    for value in (layer.width, layer.height):
        if type(value) is not float or not MIN_SIDE <= value <= MAX_SIDE:
            msg = "A page side must be a number of points between 3 and 14,400."
            raise ValueError(msg)
    unmapped = layer.unmapped
    if type(unmapped) is not int or not 0 <= unmapped <= counts(text)[1]:
        msg = "The unmapped glyphs must be a count no larger than the page's readable characters."
        raise ValueError(msg)
    return layer


def judge(text: str, layer: PageLayer) -> TextLayer:
    """The outcome of the rules above for a page with ``text`` and the measurements ``layer``.
    ``ValueError`` (from ``check_layer``) for measurements the extractor cannot give."""
    check_layer(text, layer)
    visible, readable = counts(text)
    if visible == 0:
        return TextLayer.EMPTY
    if 2 * (readable - layer.unmapped) < visible:
        return TextLayer.UNREADABLE
    if visible * A4_AREA < MIN_VISIBLE_PER_A4 * layer.width * layer.height:
        return TextLayer.SPARSE
    return TextLayer.USABLE
