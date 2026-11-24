"""The PDF extractor (``vault.ingest.extractors.pdf``), on synthetic PDFs built in the tests."""

from __future__ import annotations

import contextlib
import logging
import random
import re
import threading
import time
from pathlib import Path

import pypdfium2
import pypdfium2.raw as pdfium_c
import pytest

from tests.unit.pdf_builder import Bookmark, build, text_stream, to_unicode_map, utf16
from vault.db.pages import PageHeading, PageText
from vault.ingest.chunking.structural_v1 import chunk_pages
from vault.ingest.extractors import Extraction, ExtractionError, Heading, Page, PageLayer
from vault.ingest.extractors import pdf as extractor
from vault.ingest.extractors.pdf import (
    EmptyPdfError,
    EncryptedPdfError,
    NotPdfError,
    PdfLimitError,
    extract,
)
from vault.ingest.validate import DEFAULT_LIMITS, Limits, Reason
from vault.jobs import sandbox, sandbox_child
from vault.jobs.sandbox import EntryPoint, SandboxChildError, SandboxLimits

# Words that stand for a document's private content and its file name: no error, log line or
# console output may hold them.
SECRET = "Confidential diagnosis"  # noqa: S105 (document text, not a password)
FILE_NAME = "Patient-Name.pdf"
ENTRY = "vault.ingest.extractors.pdf:extract"


def texts(data: bytes, limits: Limits = DEFAULT_LIMITS) -> list[str]:
    return [page.text for page in extract(data, limits).pages]


# --- What the text holds -------------------------------------------------------------------------


def test_one_page_per_pdf_page_in_order_with_the_version() -> None:
    data = build([text_stream(["First page"]), text_stream(["Second page"])])

    result = extract(data)

    letter = PageLayer(612.0, 792.0, 0)
    assert result == Extraction(
        "pdf-3",
        (Page(1, "First page", layer=letter), Page(2, "Second page", layer=letter)),
        encoding=None,
    )
    assert all(page.headings == () for page in result.pages)


def test_line_breaks_are_newlines() -> None:
    assert texts(build([text_stream(["one", "two", "three"])])) == ["one\ntwo\nthree"]


def test_a_word_hyphenated_at_a_line_end_is_joined() -> None:
    # PDFium marks the hyphen with U+FFFE and drops the line break after it.
    assert texts(build([text_stream(["An exam-", "ple here"])])) == ["An example here"]


def test_a_hyphen_inside_a_line_is_kept() -> None:
    assert texts(build([text_stream(["self-made plans"])])) == ["self-made plans"]


def test_a_page_with_no_text_layer_is_an_empty_page() -> None:
    data = build([text_stream(["Typed"]), None, text_stream(["Typed again"])])

    assert texts(data) == ["Typed", "", "Typed again"]


@pytest.mark.parametrize("lines", [[" "], ["   ", "  "], ["\t"]])
def test_a_page_of_only_white_space_is_an_empty_page(lines: list[str]) -> None:
    assert texts(build([text_stream(lines)])) == [""]


def test_a_scan_with_no_text_on_any_page_is_still_read_as_empty_pages() -> None:
    assert texts(build([None, None])) == ["", ""]


def test_characters_beyond_the_basic_plane_come_through() -> None:
    mapping = to_unicode_map({0x41: "D83DDE00", 0x42: "00E9", 0x43: "4E2D"})

    assert texts(build([text_stream(["ABC"])], to_unicode=mapping)) == ["\U0001f600é中"]


def test_characters_that_are_not_text_are_removed() -> None:
    # A broken font map can give NUL and other control characters (PDFium itself replaces a map
    # to U+FFFF with the character code, so that one is covered by test_clean).
    mapping = to_unicode_map({0x41: "0041", 0x42: "0000", 0x43: "0007", 0x44: "001B", 0x45: "0085"})

    assert texts(build([text_stream(["ABCDEA"])], to_unicode=mapping)) == ["AA"]


def test_half_of_a_surrogate_pair_becomes_a_replacement_character() -> None:
    mapping = to_unicode_map({0x41: "0041", 0x42: "D83D"})

    (text,) = texts(build([text_stream(["ABA"])], to_unicode=mapping))

    assert "\ud83d" not in text
    assert text.startswith("A")
    assert text.endswith("A")
    text.encode("utf-8")  # storable: no lone surrogate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb\rc\nd", "a\nb\nc\nd"),
        ("exam￾ple", "example"),
        ("tab\tkept", "tab\tkept"),
        ("\x00\x01\x08\x0b\x0c\x0e\x1f\x7f\x80\x9f￿x", "x"),
        (" \r\n\t ", ""),
        ("　", ""),
        ("  text  ", "  text  "),
    ],
)
def test_clean(raw: str, expected: str) -> None:
    assert extractor.clean(raw) == expected


def test_every_text_pdf_of_the_demo_corpus_reads_as_its_reference_text() -> None:
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
        result = extract(path.read_bytes())
        reference = (references / path.relative_to(corpus)).with_suffix(".txt")
        words = " ".join(page.text for page in result.pages).split()
        assert words == reference.read_text(encoding="utf-8").split()
        assert all(page.text for page in result.pages)
    for path in scans:
        result = extract(path.read_bytes())
        assert result.pages
        assert all(page.text == "" for page in result.pages)


# --- Headings from the outline -------------------------------------------------------------------

B = Bookmark


def headings(data: bytes) -> list[list[tuple[int, str, str]]]:
    """Each page's headings as (level, title, the text they span)."""
    return [
        [(h.level, h.title, page.text[h.start : h.end]) for h in page.headings]
        for page in extract(data).pages
    ]


def test_a_pdf_with_no_outline_or_an_empty_one_has_no_headings() -> None:
    pages: list[bytes | None] = [text_stream(["Introduction", "Text."])]

    assert headings(build(pages)) == [[]]
    assert headings(build(pages, outline=[])) == [[]]


def test_each_bookmark_is_a_heading_on_its_line_at_the_level_of_its_depth() -> None:
    data = build(
        [
            text_stream(["Running head", "1 Introduction", "Text.", "1.1 Scope", "More."]),
            text_stream(["  2   METHODS  ", "Text."]),
        ],
        outline=[
            B(b"1 Introduction", page=0, kids=(B(b"1.1 Scope", page=0),)),
            B(b" 2  Methods ", page=1),  # white space collapsed and case folded to match
        ],
    )

    assert headings(data) == [
        [(1, "1 Introduction", "1 Introduction"), (2, "1.1 Scope", "1.1 Scope")],
        [(1, "2 Methods", "2 METHODS")],
    ]


def test_a_title_not_found_on_its_page_starts_its_section_at_the_top() -> None:
    data = build(
        [text_stream(["Running head", "Text."]), text_stream(["Other", "Text."])],
        outline=[B(b"Chapter One", page=0), B(b"Chapter Two", page=1)],
    )

    assert headings(data) == [
        [(1, "Chapter One", "Running head")],
        [(1, "Chapter Two", "Other")],
    ]


def test_a_title_not_found_takes_the_line_after_the_heading_before_it() -> None:
    data = build(
        [text_stream(["Part A", "Text one.", "Text two.", "Part C", "Text three."])],
        outline=[B(b"Part A", page=0), B(b"Part B", page=0), B(b"Part C", page=0)],
    )

    assert headings(data) == [
        [(1, "Part A", "Part A"), (1, "Part B", "Text one."), (1, "Part C", "Part C")]
    ]


def test_a_title_not_found_with_no_line_before_the_next_match_is_left_out() -> None:
    data = build(
        [text_stream(["Part B", "Text."])],
        outline=[B(b"Part A", page=0), B(b"Part B", page=0), B(b"Part C", page=0)],
    )

    # Part A has no line before Part B's; Part C takes the line after Part B.
    assert headings(data) == [[(1, "Part B", "Part B"), (1, "Part C", "Text.")]]


def test_more_bookmarks_than_lines_keep_those_that_fit() -> None:
    data = build(
        [text_stream(["Only line"])],
        outline=[B(b"One", page=0), B(b"Two", page=0)],
    )

    assert headings(data) == [[(1, "One", "Only line")]]


def test_matches_follow_the_outline_order_on_a_page() -> None:
    # The second bookmark's title comes before the first's in the text: it is not matched above
    # the first, so it takes the line after it.
    data = build(
        [text_stream(["Beta", "Text.", "Alpha", "Text."])],
        outline=[B(b"Alpha", page=0), B(b"Beta", page=0)],
    )

    assert headings(data) == [[(1, "Alpha", "Alpha"), (1, "Beta", "Text.")]]


def test_a_repeated_title_matches_the_next_line_that_holds_it() -> None:
    data = build(
        [text_stream(["Notes", "Text.", "Notes", "Text."])],
        outline=[B(b"Notes", page=0), B(b"Notes", page=0)],
    )

    page = extract(data).pages[0]
    assert [(h.start, h.end) for h in page.headings] == [(0, 5), (12, 17)]


def test_bookmarks_out_of_page_order_become_headings_in_page_order() -> None:
    data = build(
        [text_stream(["First", "a."]), text_stream(["Second", "b."]), text_stream(["Third"])],
        outline=[B(b"Third", page=2), B(b"First", page=0, kids=(B(b"Second", page=1),))],
    )

    assert headings(data) == [
        [(1, "First", "First")],
        [(2, "Second", "Second")],
        [(1, "Third", "Third")],
    ]


def test_the_section_paths_of_the_chunks_follow_the_outline() -> None:
    data = build(
        [
            text_stream(["Cover text."]),
            text_stream(["Chapter 1", "Opening.", "Section 1.1", "Detail."]),
            text_stream(["Continued detail."]),
            text_stream(["Chapter 2", "Closing."]),
        ],
        outline=[
            B(b"Chapter 1", page=1, kids=(B(b"Section 1.1", page=1),)),
            B(b"Chapter 2", page=3),
        ],
    )
    pages = [
        PageText(page.number, page.text, tuple(_stored(h) for h in page.headings))
        for page in extract(data).pages
    ]

    assert [(c.page_start, c.section_path, c.text) for c in chunk_pages(pages)] == [
        (1, (), "Cover text."),
        (2, ("Chapter 1",), "Chapter 1\nOpening."),
        (2, ("Chapter 1", "Section 1.1"), "Section 1.1\nDetail."),
        (3, ("Chapter 1", "Section 1.1"), "Continued detail."),
        (4, ("Chapter 2",), "Chapter 2\nClosing."),
    ]


def _stored(heading: Heading) -> PageHeading:
    return PageHeading(heading.level, heading.title, heading.start, heading.end)


@pytest.mark.parametrize(
    ("bookmark", "found"),
    [
        pytest.param(B(b"Here", page=1), True, id="a destination array"),
        pytest.param(B(b"Here", dest="[{page1} 0 R /Fit]"), True, id="another view"),
        pytest.param(B(b"Here", dest="[1 /Fit]"), True, id="a page index"),
        pytest.param(B(b"Here", dest="(second)"), True, id="a named destination"),
        pytest.param(B(b"Here", dest="/second"), True, id="a destination by a name object"),
        pytest.param(
            B(b"Here", action="<< /S /GoTo /D [{page1} 0 R /Fit] >>"), True, id="a GoTo action"
        ),
        pytest.param(B(b"Here", action="<< /S /GoTo /D (second) >>"), True, id="GoTo by name"),
        pytest.param(B(b"Here"), False, id="no destination"),
        pytest.param(B(b"Here", dest="[2 /Fit]"), False, id="a page index past the end"),
        pytest.param(B(b"Here", dest="[99 /Fit]"), False, id="far past the end"),
        pytest.param(B(b"Here", dest="[-1 /Fit]"), False, id="a negative page index"),
        pytest.param(B(b"Here", dest="[3 0 R /Fit]"), False, id="an object that is not a page"),
        pytest.param(B(b"Here", dest="[77 0 R /Fit]"), False, id="an object that does not exist"),
        pytest.param(B(b"Here", dest="(missing)"), False, id="an unknown name"),
        pytest.param(B(b"Here", dest="[]"), False, id="an empty array"),
        pytest.param(B(b"Here", dest="42"), False, id="a number"),
        pytest.param(
            B(b"Here", action="<< /S /GoToR /F (other.pdf) /D [1 /Fit] >>"),
            False,
            id="a page of another file",
        ),
        pytest.param(
            B(b"Here", dest="[1 /Fit]", action="<< /S /GoToR /F (other.pdf) /D [1 /Fit] >>"),
            False,
            id="a destination beside an action to another file",
        ),
        pytest.param(
            B(b"Here", action="<< /S /URI /URI (https://example.org/) >>"), False, id="a web link"
        ),
        pytest.param(
            B(b"Here", action="<< /S /JavaScript /JS (app.alert(1)) >>"), False, id="a script"
        ),
        pytest.param(B(b"Here", action="<< /S /GoTo >>"), False, id="GoTo with no destination"),
    ],
)
def test_a_bookmark_is_a_heading_only_on_a_page_of_this_document(
    bookmark: Bookmark, found: bool
) -> None:
    data = build(
        [text_stream(["First"]), text_stream(["Here", "Text."])],
        outline=[bookmark],
        catalog_extra="/Dests << /second [{page1} 0 R /Fit] >>",
    )

    assert headings(data) == [[], [(1, "Here", "Here")] if found else []]


def test_a_bookmark_that_is_not_a_heading_keeps_its_children() -> None:
    data = build(
        [text_stream(["Child"])],
        outline=[B(b"Parent", kids=(B(b"Child", page=0),))],
    )

    assert headings(data) == [[(2, "Child", "Child")]]


def test_a_bookmark_on_a_page_with_no_text_is_not_a_heading() -> None:
    data = build([None, text_stream(["Typed"])], outline=[B(b"Scan", page=0)])

    assert headings(data) == [[], []]


def _chain(depth: int) -> Bookmark:
    item = B(f"Level {depth}".encode(), page=0)
    for level in range(depth - 1, 0, -1):
        item = B(f"Level {level}".encode(), page=0, kids=(item,))
    return item


def test_bookmarks_deeper_than_the_deepest_level_are_not_read() -> None:
    lines = [f"Level {n}" for n in range(1, 13)]
    data = build([text_stream(lines)], outline=[_chain(12)])

    found = headings(data)[0]
    assert [(level, title) for level, title, _ in found] == [
        (n, f"Level {n}") for n in range(1, extractor.MAX_OUTLINE_DEPTH + 1)
    ]


@pytest.mark.parametrize("loop", ["sibling", "child"])
def test_an_outline_that_loops_gives_no_headings_and_the_text_is_still_read(loop: str) -> None:
    data = build(
        [text_stream(["Alpha", "Beta", "Gamma"])],
        outline=[B(b"Alpha", page=0, kids=(B(b"Beta", page=0),)), B(b"Gamma", page=0)],
        outline_loop=loop,
    )

    result = extract(data)
    assert [page.text for page in result.pages] == ["Alpha\nBeta\nGamma"]
    assert result.pages[0].headings == ()


def test_a_loop_is_given_up_when_it_comes_round_not_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = pdfium_c.FPDFBookmark_GetNextSibling

    def next_sibling(document: object, bookmark: object) -> object:
        nonlocal calls
        calls += 1
        return original(document, bookmark)

    monkeypatch.setattr(pdfium_c, "FPDFBookmark_GetNextSibling", next_sibling)
    data = build(
        [text_stream(["Alpha", "Gamma"])],
        outline=[B(b"Alpha", page=0), B(b"Gamma", page=0)],
        outline_loop="sibling",
    )

    assert headings(data) == [[]]
    assert calls <= 3


def test_a_bookmark_under_two_parents_gives_no_headings() -> None:
    # Objects 6 to 9 are the outline, Alpha, Beta and Gamma: Gamma is given Beta as a child too.
    gamma = b"/Title <47616d6d61> /Parent 6 0 R"
    data = build(
        [text_stream(["Alpha", "Beta", "Gamma"])],
        outline=[B(b"Alpha", page=0, kids=(B(b"Beta", page=0),)), B(b"Gamma", page=0)],
    )
    assert data.count(gamma) == 1

    shared = data.replace(gamma, gamma + b" /First 8 0 R /Last 8 0 R")

    assert len(headings(data)[0]) == 3
    assert headings(shared) == [[]]


def test_more_bookmarks_than_the_cap_give_no_headings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "MAX_BOOKMARKS", 3)
    pages: list[bytes | None] = [text_stream(["Alpha", "Beta", "Gamma", "Delta"])]
    three = [B(t, page=0) for t in (b"Alpha", b"Beta", b"Gamma")]

    assert [t for _, t, _ in headings(build(pages, outline=three))[0]] == ["Alpha", "Beta", "Gamma"]
    # Bookmarks that are not headings count too: each one read costs a call into PDFium.
    assert headings(build(pages, outline=[*three, B(b"Delta")])) == [[]]


def test_an_outline_over_the_real_cap_is_given_up_quickly() -> None:
    items = [B(f"Item {n}".encode(), page=0) for n in range(extractor.MAX_BOOKMARKS + 1)]
    data = build([text_stream(["Item 0"])], outline=items)

    assert headings(data) == [[]]


def test_titles_over_the_cap_in_all_give_no_headings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor, "MAX_TITLE_CHARS", 6)
    pages: list[bytes | None] = [text_stream(["Abc", "Def", "Ghi"])]

    assert len(headings(build(pages, outline=[B(b"Abc", page=0), B(b"Def", page=0)]))[0]) == 2
    many = [B(b"Abc", page=0), B(b"Def", page=0), B(b"G", page=0)]
    assert headings(build(pages, outline=many)) == [[]]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        pytest.param(b"Plain", "Plain", id="PDFDocEncoding"),
        pytest.param(utf16("Café \U0001f600"), "Café \U0001f600", id="UTF-16"),
        pytest.param(b"\xfe\xff\xd8\x00\x00A", "�A", id="half a surrogate pair"),
        pytest.param(b"Tab\there\r\nand\x00more\x01", "Tab here and more", id="controls"),
        pytest.param(utf16("a\x1fb\x7fc"), "a b c", id="C0 controls and delete"),
        pytest.param(utf16("a\u0085b\u009fc"), "a b c", id="C1 controls"),
        pytest.param(utf16("x￾y￿z"), "x y z", id="noncharacters"),
        pytest.param(b"  spaced   out  ", "spaced out", id="white space"),
        pytest.param(b"", None, id="empty"),
        pytest.param(b"   ", None, id="only spaces"),
        pytest.param(b"\x01\x02\r\n", None, id="only control characters"),
        pytest.param(utf16(""), None, id="only a byte order mark"),
    ],
)
def test_a_title_is_cleaned_and_a_blank_one_is_not_a_heading(
    title: bytes, expected: str | None
) -> None:
    data = build([text_stream(["Line"])], outline=[B(title, page=0)])

    found = headings(data)[0]
    assert found == ([] if expected is None else [(1, expected, "Line")])


def test_a_title_longer_than_the_cap_is_left_out_not_cut() -> None:
    cap = extractor.MAX_TITLE_UNITS
    at_cap = "a" * cap
    # A character outside the BMP is two UTF-16 units, so this is one unit over the cap.
    over = "a" * (cap - 1) + "\U0001f600"
    data = build(
        [text_stream(["Line one", "Line two"])],
        outline=[B(utf16(at_cap), page=0), B(utf16(over), page=0, kids=(B(b"Kid", page=0),))],
    )

    found = headings(data)[0]
    assert [(level, len(title)) for level, title, _ in found] == [(1, cap), (2, 3)]


def test_a_long_title_is_not_copied_out_of_pdfium(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []
    original = pdfium_c.FPDFBookmark_GetTitle

    def get_title(bookmark: object, buffer: object, size: int) -> int:
        sizes.append(size)
        return int(original(bookmark, buffer, size))

    monkeypatch.setattr(pdfium_c, "FPDFBookmark_GetTitle", get_title)
    title = utf16("a" * (extractor.MAX_TITLE_UNITS + 1))
    assert headings(build([text_stream(["Line"])], outline=[B(title, page=0)])) == [[]]
    assert sizes == [0]  # only the size was asked for


def test_a_section_number_on_one_side_only_still_matches() -> None:
    data = build(
        [text_stream(["Header", "1.2 Results", "Text.", "Discussion", "Text."])],
        outline=[B(b"Results", page=0), B(b"3. Discussion", page=0)],
    )

    assert headings(data) == [[(1, "Results", "1.2 Results"), (1, "3. Discussion", "Discussion")]]


@pytest.mark.parametrize(
    ("line", "title", "same"),
    [
        ("results", "results", True),
        ("1.2 results", "results", True),
        ("results", "1.2 results", True),
        ("1.2. results", "results", True),
        ("12) results", "results", True),
        ("3: results", "results", True),
        ("iv. results", "results", True),
        ("a) results", "results", True),
        ("b. results", "results", True),
        ("1.2 results", "2 results", False),
        ("1 results", "2 results", False),  # both numbered: only an equal number matches
        ("1.2results", "results", False),
        ("did results", "results", False),  # a word that is also a Roman numeral, no mark
        ("a results", "results", False),
        ("1..2 results", "results", False),
        ("1.2 ", "", False),
        ("1.2", "", False),
        ("results 1.2", "results", False),
        ("results", "result", False),
    ],
)
def test_same(line: str, title: str, same: bool) -> None:
    assert extractor._same(line, title) is same


def _place_by_comparing_every_line(text: str, bookmarks: list[tuple[int, str]]) -> list[Heading]:
    """The rule of ``place`` written the slow way, line by line with ``_same``."""
    lines = []
    for match in re.finditer(r"[^\n]+", text):
        start, end = match.start(), match.end()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            lines.append((start, end, " ".join(text[start:end].split()).casefold()))
    matched: list[int | None] = []
    after = 0
    for _level, title in bookmarks:
        found = [
            n for n in range(after, len(lines)) if extractor._same(lines[n][2], title.casefold())
        ]
        matched.append(found[0] if found else None)
        after = found[0] + 1 if found else after
    placed, previous = [], -1
    for position, (level, title) in enumerate(bookmarks):
        line = matched[position]
        if line is None:
            later = [n for n in matched[position + 1 :] if n is not None]
            if previous + 1 >= (later[0] if later else len(lines)):
                continue
            line = previous + 1
        placed.append(Heading(level, title, lines[line][0], lines[line][1]))
        previous = line
    return placed


def test_place_agrees_with_comparing_every_line() -> None:
    generator = random.Random(15)  # noqa: S311 (test data, not a secret)
    words = ["1", "1.2", "iv.", "a)", "did", "Results", "results", "Intro", " ", "\t", "\n", "x"]
    for _ in range(3000):
        text = "".join(generator.choice(words) + generator.choice([" ", "\n"]) for _ in range(8))
        titles = [
            " ".join(generator.choice(words[:8]) for _ in range(generator.randint(1, 2)))
            for _ in range(generator.randint(0, 5))
        ]
        bookmarks = [(generator.randint(1, 9), extractor.clean_title(t)) for t in titles]
        bookmarks = [(level, title) for level, title in bookmarks if title]
        assert list(extractor.place(text, bookmarks)) == _place_by_comparing_every_line(
            text, bookmarks
        )


def test_the_largest_page_with_every_bookmark_is_placed_quickly() -> None:
    text = "line\n" * (DEFAULT_LIMITS.max_pdf_text_chars // 5)
    found = [(1, "line")] * (extractor.MAX_BOOKMARKS // 2)
    missing = [(1, "absent")] * (extractor.MAX_BOOKMARKS // 2)
    bookmarks = [item for pair in zip(missing, found, strict=True) for item in pair]

    started = time.monotonic()
    placed = extractor.place(text, bookmarks)

    # Each missing title has no line left before the next match, so only the found ones stay.
    assert len(placed) == extractor.MAX_BOOKMARKS // 2
    assert time.monotonic() - started < 10


@pytest.mark.parametrize(
    ("text", "bookmarks", "expected"),
    [
        pytest.param("", [(1, "A")], (), id="no text"),
        pytest.param("A\nB", [], (), id="no bookmarks"),
        pytest.param("\n\n  A  \n", [(1, "A")], (Heading(1, "A", 4, 5),), id="blank lines"),
        pytest.param(
            "x\n\ty\t",
            [(1, "Q"), (2, "R")],
            (Heading(1, "Q", 0, 1), Heading(2, "R", 3, 4)),
            id="none found",
        ),
        pytest.param(
            "a\nB", [(1, "b"), (1, "A")], (Heading(1, "b", 2, 3),), id="first found, then no room"
        ),
    ],
)
def test_place(text: str, bookmarks: list[tuple[int, str]], expected: tuple[Heading, ...]) -> None:
    assert extractor.place(text, bookmarks) == expected


# --- Refused files -------------------------------------------------------------------------------


def test_a_pdf_that_needs_a_password_is_refused() -> None:
    data = build([text_stream([SECRET])], user_password=b"secret")

    with pytest.raises(EncryptedPdfError):
        extract(data)


def test_a_pdf_with_an_unsupported_security_handler_is_refused_as_encrypted() -> None:
    data = build([text_stream([SECRET])], user_password=b"", security_filter="Unknown")

    with pytest.raises(EncryptedPdfError):
        extract(data)


def test_no_password_is_ever_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    load = pdfium_c.FPDF_LoadMemDocument64

    def recording(data: bytes, size: int, password: object) -> object:
        calls.append(password)
        return load(data, size, password)

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", recording)
    with pytest.raises(EncryptedPdfError):
        extract(build([text_stream(["x"])], user_password=b"secret"))
    extract(build([text_stream(["x"])]))

    assert calls == [None, None]


def test_a_pdf_with_an_owner_password_only_is_read() -> None:
    # It opens without a password and only restricts what a viewer may do with it.
    data = build([text_stream(["Readable text"])], user_password=b"")

    assert texts(data) == ["Readable text"]


def test_a_pdf_with_no_pages_is_refused() -> None:
    with pytest.raises(EmptyPdfError):
        extract(build([]))


def test_no_pages_after_an_encrypted_file_is_still_no_pages() -> None:
    # PDFium keeps its last error after a document opens, so reading the error for a document
    # that opened with no pages would report the earlier file's password error.
    with pytest.raises(EncryptedPdfError):
        extract(build([text_stream(["x"])], user_password=b"secret"))

    with pytest.raises(EmptyPdfError):
        extract(build([]))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"%PDF-1.4\n" + SECRET.encode(), id="garbage after the header"),
        pytest.param(build([text_stream([SECRET])])[:60], id="cut short"),
        pytest.param(b"%PDF-1.7\n" + b"\x00" * 4096, id="zeros"),
        pytest.param(
            build([text_stream([SECRET])]).replace(b"/Root 1 0 R", b"/Root 9 0 R"), id="no root"
        ),
    ],
)
def test_a_corrupt_pdf_is_refused(data: bytes) -> None:
    with pytest.raises(NotPdfError) as caught:
        extract(data)

    assert caught.value.reason is None


def test_a_page_the_tree_claims_but_does_not_hold_refuses_the_file() -> None:
    data = build([text_stream(["Only page"])], claimed_pages=3)

    with pytest.raises(NotPdfError):
        extract(data)


def test_a_page_whose_text_pdfium_cannot_load_refuses_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(_self: object) -> None:
        raise pypdfium2.PdfiumError(SECRET)

    monkeypatch.setattr(pypdfium2.PdfPage, "get_textpage", failing)

    with pytest.raises(NotPdfError) as caught:
        extract(build([text_stream(["x"])]))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", Reason.EMPTY),
        (b"PK\x03\x04 not a pdf", Reason.CONTENT_MISMATCH),
        (b"hello %PDF-1.4", Reason.CONTENT_MISMATCH),
        (SECRET.encode(), Reason.CONTENT_MISMATCH),
    ],
)
def test_bytes_that_are_not_a_pdf_are_refused_before_pdfium_sees_them(
    data: bytes, reason: Reason, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_args: object) -> None:
        raise AssertionError

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", never)

    with pytest.raises(NotPdfError) as caught:
        extract(data)

    assert caught.value.reason is reason


def test_bytes_over_the_size_limit_are_refused_before_pdfium_sees_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = build([text_stream(["x"])])

    def never(*_args: object) -> None:
        raise AssertionError

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", never)

    with pytest.raises(NotPdfError) as caught:
        extract(data, Limits(max_bytes=len(data) - 1))

    assert caught.value.reason is Reason.TOO_LARGE


def test_the_header_may_follow_a_little_white_space() -> None:
    assert texts(b"\r\n" + build([text_stream(["after white space"])])) == ["after white space"]


# --- Limits --------------------------------------------------------------------------------------


def test_more_pages_than_the_limit_are_refused_before_any_page_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = build([text_stream(["a"]), text_stream(["b"]), text_stream(["c"])])

    def never(*_args: object) -> None:
        raise AssertionError

    monkeypatch.setattr(extractor, "_page_text", never)

    with pytest.raises(PdfLimitError) as caught:
        extract(data, Limits(max_pdf_pages=2))
    assert caught.value.limit == "pages"


def test_exactly_the_page_limit_is_read() -> None:
    data = build([text_stream(["a"]), text_stream(["b"])])

    assert texts(data, Limits(max_pdf_pages=2)) == ["a", "b"]


def test_more_text_than_the_limit_is_refused_before_the_page_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = build([text_stream(["abcde"]), text_stream(["fghij"])])
    read: list[int] = []
    original = pypdfium2.PdfTextPage.get_text_range

    def counting(self: pypdfium2.PdfTextPage, *args: object, **kwargs: object) -> str:
        read.append(1)
        return str(original(self, *args, **kwargs))

    monkeypatch.setattr(pypdfium2.PdfTextPage, "get_text_range", counting)

    with pytest.raises(PdfLimitError) as caught:
        extract(data, Limits(max_pdf_text_chars=9))
    assert caught.value.limit == "text"
    assert read == [1]  # the first page only: the second's count was over what was left


def test_exactly_the_text_limit_is_read() -> None:
    data = build([text_stream(["abcde"]), text_stream(["fghij"])])

    assert texts(data, Limits(max_pdf_text_chars=10)) == ["abcde", "fghij"]


def test_text_pdfium_adds_beyond_its_count_is_counted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    data = build([text_stream(["abc"])])

    def longer(_self: object, *_args: object, **_kwargs: object) -> str:
        return "abcdefgh"

    monkeypatch.setattr(pypdfium2.PdfTextPage, "get_text_range", longer)

    with pytest.raises(PdfLimitError):
        extract(data, Limits(max_pdf_text_chars=5))
    assert texts(data, Limits(max_pdf_text_chars=8)) == ["abcdefgh"]


@pytest.mark.parametrize(
    "character",
    [
        pytest.param(",", id="a value mark for the sandbox"),
        pytest.param("\U0001f600", id="twelve bytes of JSON"),
        pytest.param('"', id="an escaped quote"),
    ],
)
def test_the_largest_result_the_limits_allow_fits_the_sandbox_defaults(character: str) -> None:
    # The worst case for the sandbox's answer: every page there may be, the whole text cap
    # spent on the character that costs the most, every page number at its widest, and each
    # page's measurements at their longest (a side of 17 significant digits, every glyph counted),
    # with every heading the outline caps allow, their titles spending the whole title cap on
    # that character too.
    limits = DEFAULT_LIMITS
    per_page, rest = divmod(limits.max_pdf_text_chars, limits.max_pdf_pages)
    side = 14_399.999999999998
    per_heading = extractor.MAX_TITLE_CHARS // extractor.MAX_BOOKMARKS
    headings_per_page = -(-extractor.MAX_BOOKMARKS // limits.max_pdf_pages)
    assert per_heading <= extractor.MAX_TITLE_UNITS
    assert per_page >= 2 * headings_per_page
    # The same span on every heading of a page: only the size of the answer is judged here.
    heading = Heading(extractor.MAX_OUTLINE_DEPTH, character * per_heading, per_page - 2, per_page)
    pages = tuple(
        Page(
            number,
            character * (size := per_page + (1 if number <= rest else 0)),
            (heading,) * headings_per_page,
            layer=PageLayer(side, side, size),
        )
        for number in range(1, limits.max_pdf_pages + 1)
    )
    assert sum(len(page.headings) for page in pages) >= extractor.MAX_BOOKMARKS
    result = Extraction(extractor.VERSION, pages)
    status, payload = sandbox_child.OK, sandbox_child.encode(result)
    answer = status + payload

    assert len(answer) <= sandbox.DEFAULT_MAX_RESULT_BYTES
    cost, values = sandbox._answer_cost(answer)
    assert cost <= sandbox.DEFAULT_MAX_RESULT_MEMORY_BYTES
    assert values <= sandbox.DEFAULT_MAX_RESULT_VALUES
    decoded = sandbox._interpret(answer, EntryPoint(ENTRY, Extraction)._decoder)
    assert decoded == result


# --- Nothing private leaves the extractor --------------------------------------------------------


REFUSED = [
    pytest.param(build([text_stream([SECRET])], user_password=b"pw"), id="encrypted"),
    pytest.param(build([]), id="no pages"),
    pytest.param(b"%PDF-1.4\n" + SECRET.encode() + b"\n" + FILE_NAME.encode(), id="corrupt"),
    pytest.param(build([text_stream([SECRET])], claimed_pages=2), id="missing page"),
    pytest.param(SECRET.encode(), id="not a pdf"),
]


@pytest.mark.parametrize("data", REFUSED)
def test_an_error_quotes_nothing_and_keeps_no_library_exception(data: bytes) -> None:
    with pytest.raises(ExtractionError) as caught:
        extract(data)

    error = caught.value
    message = str(error)
    assert SECRET not in message
    assert FILE_NAME not in message
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_a_limit_error_quotes_nothing() -> None:
    with pytest.raises(PdfLimitError) as caught:
        extract(build([text_stream([SECRET])]), Limits(max_pdf_text_chars=3))

    assert SECRET not in str(caught.value)
    assert str(caught.value) == "The PDF is over the text limit."


def _secret_outline(**options: object) -> bytes:
    kids = (B(SECRET.encode(), page=0, kids=(B(FILE_NAME.encode(), page=0),)),)
    return build(
        [text_stream([SECRET])],
        outline=[B(SECRET.encode(), page=0, kids=kids), B(utf16(SECRET * 100), dest="[5 /Fit]")],
        **options,  # type: ignore[arg-type]
    )


OUTLINES = [
    pytest.param(_secret_outline(), id="an outline"),
    pytest.param(_secret_outline(outline_loop="sibling"), id="an outline that loops"),
    pytest.param(_secret_outline(outline_loop="child"), id="an outline that loops down"),
    pytest.param(build([text_stream([SECRET])], outline=[_chain(20)]), id="a deep outline"),
    pytest.param(
        build(
            [text_stream([SECRET])],
            outline=[B(SECRET.encode(), page=0) for _ in range(extractor.MAX_BOOKMARKS + 1)],
        ),
        id="an outline over the cap",
    ),
]


@pytest.mark.parametrize(
    "data", [*REFUSED, pytest.param(build([text_stream([SECRET])]), id="ok"), *OUTLINES]
)
def test_nothing_reaches_the_console_or_the_log(
    data: bytes, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with contextlib.suppress(ExtractionError):
        extract(data)

    out, err = capfd.readouterr()
    assert out == ""
    assert err == ""
    assert caplog.records == []


# --- Running it ----------------------------------------------------------------------------------


def test_calls_from_several_threads_give_each_its_own_result() -> None:
    documents = [build([text_stream([f"Document {n}"]) for _ in range(5)]) for n in range(8)]
    results: dict[int, list[str]] = {}

    def work(n: int) -> None:
        results[n] = texts(documents[n])

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == {n: [f"Document {n}"] * 5 for n in range(8)}


SANDBOX_LIMITS = SandboxLimits(timeout_seconds=60, memory_bytes=512 * sandbox.MIB)


def test_it_runs_in_the_sandbox_as_the_pdf_entry_point() -> None:
    assert sandbox.ENTRY_POINTS["pdf"] == EntryPoint(ENTRY, Extraction)
    data = build([text_stream(["In the child"]), None], outline=[B(b"In the child", page=0)])

    result = sandbox.run("pdf", data, SANDBOX_LIMITS)

    letter = PageLayer(612.0, 792.0, 0)
    heading = Heading(1, "In the child", 0, 12)
    assert result == Extraction(
        "pdf-3", (Page(1, "In the child", (heading,), letter), Page(2, "", layer=letter))
    )


@pytest.mark.parametrize(
    ("data", "error_class"),
    [
        (build([text_stream([SECRET])], user_password=b"pw"), "EncryptedPdfError"),
        (build([]), "EmptyPdfError"),
        (b"%PDF-1.4\n" + SECRET.encode(), "NotPdfError"),
    ],
    ids=["encrypted", "no pages", "corrupt"],
)
def test_a_refusal_in_the_sandbox_comes_back_as_its_class_name(
    data: bytes, error_class: str
) -> None:
    with pytest.raises(SandboxChildError) as caught:
        sandbox.run("pdf", data, SANDBOX_LIMITS)

    assert caught.value.error_class == error_class
    assert SECRET not in str(caught.value)
