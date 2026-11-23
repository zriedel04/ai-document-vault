"""The OCR decision for a PDF page (``vault.ingest.text_layer``), on its own and on synthetic PDFs
read by the PDF extractor (``tests.unit.pdf_builder``)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tests.unit.pdf_builder import build, text_stream, to_unicode_map
from vault.db.schema import TEXT_LAYERS
from vault.ingest.extractors import PageLayer
from vault.ingest.extractors.pdf import extract
from vault.ingest.text_layer import (
    A4_AREA,
    MAX_SIDE,
    MIN_SIDE,
    MIN_VISIBLE_PER_A4,
    NEEDS_OCR,
    TextLayer,
    check_layer,
    counts,
    judge,
    side,
)

A4 = PageLayer(595.0, 842.0, 0)
LETTER = (612.0, 792.0)
SENTENCE = "The survey began in early spring, when the water was high."


def layers(data: bytes) -> list[TextLayer]:
    """The decision for each page of the PDF in ``data``, as the extraction stage makes it."""
    result = []
    for page in extract(data).pages:
        assert page.layer is not None
        result.append(judge(page.text, check_layer(page.text, page.layer)))
    return result


def test_the_values_are_the_ones_the_database_stores() -> None:
    assert tuple(TextLayer) == TEXT_LAYERS
    assert {TextLayer.EMPTY, TextLayer.SPARSE, TextLayer.UNREADABLE} == NEEDS_OCR
    assert TextLayer.USABLE not in NEEDS_OCR
    assert TextLayer.UNJUDGED not in NEEDS_OCR  # pdf-1's mark: judged when extracted again


def test_a4_is_the_area_the_density_is_counted_in() -> None:
    assert A4_AREA == 595.0 * 842.0
    assert MIN_VISIBLE_PER_A4 == 10


# --- The rules on their own ----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", " ", "\n\t \u3000\u00a0"])
def test_a_page_with_no_visible_character_is_empty(text: str) -> None:
    assert judge(text, A4) is TextLayer.EMPTY


def test_a_page_of_text_is_usable() -> None:
    assert judge(SENTENCE * 3, A4) is TextLayer.USABLE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Exactly half readable is usable; one readable character fewer is not.
        pytest.param("ab..", TextLayer.USABLE, id="half"),
        pytest.param("a...", TextLayer.UNREADABLE, id="under half"),
        pytest.param("abc..", TextLayer.USABLE, id="over half"),
        pytest.param("\ufffd" * 5 + "abcde", TextLayer.USABLE, id="replacement characters, half"),
        pytest.param("\ufffd" * 6 + "abcd", TextLayer.UNREADABLE, id="replacement characters"),
        pytest.param("\ue000" * 6 + "abcd", TextLayer.UNREADABLE, id="private use"),
        pytest.param("\U000f0000" * 6 + "abcd", TextLayer.UNREADABLE, id="private use plane 15"),
        pytest.param("\u0378" * 6 + "abcd", TextLayer.UNREADABLE, id="unassigned"),
        pytest.param("1,234.50 | 9.75 | 10.00", TextLayer.USABLE, id="a row of figures"),
    ],
)
def test_a_page_that_is_mostly_not_letters_digits_or_marks_is_unreadable(
    text: str, expected: TextLayer
) -> None:
    # Each text is repeated so that the density rule is never the one that decides.
    assert judge(text * 10, A4) is expected


def test_marks_count_as_readable() -> None:
    # Devanagari: consonants with vowel signs, which are marks (Mc, Mn).
    hindi = "\u0915\u093f\u0924\u093e\u092c \u092a\u0922\u093c\u094b"
    visible, readable = counts(hindi)
    assert visible == readable == 9
    assert judge(hindi * 5, A4) is TextLayer.USABLE


@pytest.mark.parametrize(
    ("unmapped", "expected"),
    [(10, TextLayer.USABLE), (11, TextLayer.UNREADABLE), (20, TextLayer.UNREADABLE)],
)
def test_glyphs_with_no_unicode_mapping_are_not_readable(
    unmapped: int, expected: TextLayer
) -> None:
    text = "abcdefghij" * 2  # 20 letters, of which ``unmapped`` are raw character codes
    assert judge(text, PageLayer(A4.width, A4.height, unmapped)) is expected


@pytest.mark.parametrize(
    ("visible", "expected"),
    [(MIN_VISIBLE_PER_A4, TextLayer.USABLE), (MIN_VISIBLE_PER_A4 - 1, TextLayer.SPARSE)],
)
def test_fewer_than_ten_characters_on_an_a4_page_is_sparse(
    visible: int, expected: TextLayer
) -> None:
    assert judge("x" * visible, A4) is expected
    # White space does not count.
    assert judge(" ".join("x" * visible) + "\n\n", A4) is expected


@pytest.mark.parametrize(
    ("width", "height", "visible", "expected"),
    [
        # Twice an A4 page needs twice the characters.
        (1190.0, 842.0, 20, TextLayer.USABLE),
        (1190.0, 842.0, 19, TextLayer.SPARSE),
        # Half an A4 page needs half.
        (595.0, 421.0, 5, TextLayer.USABLE),
        (595.0, 421.0, 4, TextLayer.SPARSE),
        # The largest page there is: a short paragraph is far too little for it.
        (MAX_SIDE, MAX_SIDE, 4_000, TextLayer.SPARSE),
        # The smallest page: a single character is enough.
        (MIN_SIDE, MIN_SIDE, 1, TextLayer.USABLE),
    ],
)
def test_the_density_is_measured_per_page_area(
    width: float, height: float, visible: int, expected: TextLayer
) -> None:
    assert judge("x" * visible, PageLayer(width, height, 0)) is expected


def test_an_unreadable_page_is_unreadable_before_it_is_sparse() -> None:
    assert judge("\ue000", A4) is TextLayer.UNREADABLE


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        (0.0, MIN_SIDE),
        (-612.0, MIN_SIDE),
        (2.999, MIN_SIDE),
        (3.0, 3.0),
        (612.0, 612.0),
        (14_400.0, 14_400.0),
        (14_400.5, MAX_SIDE),
        (1e300, MAX_SIDE),
        (math.inf, MIN_SIDE),
        (-math.inf, MIN_SIDE),
        (math.nan, MIN_SIDE),
    ],
)
def test_a_page_side_is_taken_between_the_limits_iso_32000_gives(
    points: float, expected: float
) -> None:
    assert side(points) == expected


@pytest.mark.parametrize(
    "layer",
    [
        pytest.param(None, id="missing"),
        pytest.param((595.0, 842.0, 0), id="not a measurement"),
        pytest.param(PageLayer(595, 842.0, 0), id="a whole number for a side"),
        pytest.param(PageLayer(2.5, 842.0, 0), id="too small"),
        pytest.param(PageLayer(595.0, 14_401.0, 0), id="too large"),
        pytest.param(PageLayer(math.nan, 842.0, 0), id="not a number"),
        pytest.param(PageLayer(595.0, 842.0, -1), id="negative count"),
        pytest.param(PageLayer(595.0, 842.0, True), id="a flag for a count"),
        pytest.param(PageLayer(595.0, 842.0, 1.0), id="a float for a count"),  # type: ignore[arg-type]
        pytest.param(PageLayer(595.0, 842.0, 4), id="more unmapped than readable"),
    ],
)
def test_a_measurement_the_extractor_cannot_give_is_refused(layer: object) -> None:
    with pytest.raises(ValueError, match=r"^[A-Z]") as caught:
        check_layer("abc.", layer)
    assert "abc" not in str(caught.value)
    with pytest.raises(ValueError, match=r"^[A-Z]"):
        judge("abc.", layer)  # type: ignore[arg-type]


# --- The extractor's measurements, on synthetic PDFs ---------------------------------------------


def test_a_text_page_is_usable() -> None:
    data = build([text_stream([SENTENCE, SENTENCE])])
    [page] = extract(data).pages
    assert page.layer == PageLayer(*LETTER, 0)
    assert layers(data) == [TextLayer.USABLE]


def test_an_empty_page_and_a_page_of_white_space_are_empty() -> None:
    assert (
        layers(build([None, text_stream([]), text_stream(["   ", "\t"])])) == [TextLayer.EMPTY] * 3
    )


def test_a_page_of_glyphs_with_no_unicode_mapping_is_unreadable() -> None:
    # The codes of "ABCD..." named with glyph names PDFium does not know: it gives the raw codes,
    # which read as capital letters, and flags each one.
    line = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    data = build([text_stream([line] * 3)], unknown_glyphs=range(0x41, 0x5B))
    [page] = extract(data).pages
    assert page.text.replace("\n", "") == line * 3  # it looks like text
    assert page.layer is not None
    assert page.layer.unmapped == 78
    assert layers(data) == [TextLayer.UNREADABLE]


def test_a_few_unmapped_glyphs_in_a_page_of_text_leave_it_usable() -> None:
    # Only "Z" has no Unicode; the rest of the line reads as text.
    line = "Measurements of the river Z"
    data = build([text_stream([line] * 3)], unknown_glyphs=range(0x5A, 0x5B))
    [page] = extract(data).pages
    assert page.layer is not None
    assert page.layer.unmapped == 3
    assert layers(data) == [TextLayer.USABLE]


def test_a_page_of_replacement_and_private_use_characters_is_unreadable() -> None:
    mapping = to_unicode_map({0x41: "E000", 0x42: "FFFD", 0x43: "0061"})
    data = build([text_stream(["ABABABABAC"] * 4)], to_unicode=mapping)
    [page] = extract(data).pages
    assert page.layer is not None
    assert page.layer.unmapped == 0  # mapped, to characters that are not text
    assert layers(data) == [TextLayer.UNREADABLE]


def test_a_mixed_pdf_is_decided_page_by_page() -> None:
    data = build(
        [
            text_stream([SENTENCE] * 3),
            None,
            text_stream(["ABCDEFGHIJ"] * 3),
            text_stream(["7"]),
            text_stream([SENTENCE] * 3),
        ],
        unknown_glyphs=range(0x41, 0x4B),
    )
    assert layers(data) == [
        TextLayer.USABLE,
        TextLayer.EMPTY,
        TextLayer.UNREADABLE,
        TextLayer.SPARSE,  # a page number alone: the text layer of a scan
        TextLayer.USABLE,
    ]


def test_a_little_real_text_on_a_huge_page_is_sparse() -> None:
    line = ["A short label for a drawing"]  # 23 visible characters
    data = build([text_stream(line), text_stream(line)], sizes=[(595.0, 842.0), (5000.0, 5000.0)])
    pages = extract(data).pages
    assert [page.layer for page in pages] == [
        PageLayer(595.0, 842.0, 0),
        PageLayer(5000.0, 5000.0, 0),
    ]
    assert layers(data) == [TextLayer.USABLE, TextLayer.SPARSE]


@pytest.mark.parametrize(
    ("line", "expected"),
    [("abcdefghij", TextLayer.USABLE), ("abcdefghi", TextLayer.SPARSE)],
)
def test_the_density_boundary_on_an_a4_pdf(line: str, expected: TextLayer) -> None:
    assert layers(build([text_stream([line])], sizes=[(595.0, 842.0)])) == [expected]


def test_a_page_size_outside_the_limits_is_taken_at_the_limit() -> None:
    data = build([text_stream(["x"]), text_stream(["x" * 10])], sizes=[(1.0, 1.0), (20000, 20000)])
    sides = [(page.layer.width, page.layer.height) for page in extract(data).pages if page.layer]
    # PDFium itself may clamp a box; whatever it gives, the measurement stays within the limits.
    assert all(MIN_SIDE <= value <= MAX_SIDE for pair in sides for value in pair)
    assert len(sides) == 2


def test_no_page_of_a_demo_text_pdf_needs_ocr_and_every_page_of_a_demo_scan_does() -> None:
    # Phase 3's "done when": text PDFs are never OCRed. The demo corpus is the synthetic set
    # closest to the owner's documents; its text PDFs are the ones with a reference text.
    corpus = Path(__file__).resolve().parents[3] / "demo" / "corpus"
    references = corpus / "references"
    files = sorted(corpus.rglob("*.pdf"))
    text_pdfs = [
        p for p in files if (references / p.relative_to(corpus)).with_suffix(".txt").exists()
    ]
    scans = [p for p in files if p not in text_pdfs]
    assert text_pdfs
    assert scans
    for path in text_pdfs:
        assert set(layers(path.read_bytes())) == {TextLayer.USABLE}
    for path in scans:
        assert set(layers(path.read_bytes())) <= NEEDS_OCR
