import codecs
import contextlib
from pathlib import Path

import pytest

from vault.ingest.extractors import ExtractionError, Heading, NotTextError
from vault.ingest.extractors.text import (
    FALLBACK_ENCODING,
    FALLBACK_ENCODINGS,
    VERSION,
    decode,
    extract,
    markdown_headings,
)
from vault.ingest.validate import TEXT_BOMS, Kind, sniff

# Letters outside ASCII, from code points: e acute, an em dash, and the CJK word for resume.
WORDS = "café — 履歴書"
CANARY = "canary-secret-xyzzy"


def _no_error() -> contextlib.AbstractContextManager[None]:
    return contextlib.nullcontext()


def titles(text: str) -> list[tuple[int, str]]:
    return [(heading.level, heading.title) for heading in markdown_headings(text)]


# Decoding


@pytest.mark.parametrize(("bom", "encoding"), TEXT_BOMS)
def test_a_byte_order_mark_names_the_encoding_and_is_not_part_of_the_text(
    bom: bytes, encoding: str
) -> None:
    text, used = decode(bom + f"{WORDS}\n".encode(encoding))

    assert text == f"{WORDS}\n"
    assert used == codecs.lookup(encoding).name


def test_utf8_without_a_mark_is_read_as_utf8() -> None:
    assert decode(WORDS.encode()) == (WORDS, "utf-8")


def test_bytes_that_are_not_utf8_are_read_as_windows_1252() -> None:
    # What Notepad wrote as "ANSI": e acute is one byte, and 0x80 is the euro sign.
    assert decode(b"caf\xe9 \x80 5") == ("café € 5", "cp1252")
    assert FALLBACK_ENCODING == "cp1252"


@pytest.mark.parametrize(
    ("text", "fallback"),
    [
        # Russian, Greek and Japanese notes, from code points.
        ("\u041f\u0440\u0438\u0432\u0435\u0442, \u043e\u0442\u0447\u0451\u0442", "cp1251"),
        ("\u039a\u03b1\u03bb\u03b7\u03bc\u03ad\u03c1\u03b1", "cp1253"),
        ("\u5831\u544a\u66f8\u3092\u6301\u3063\u3066", "cp932"),
    ],
)
def test_the_owner_can_choose_another_windows_code_page(text: str, fallback: str) -> None:
    data = text.encode(fallback)

    assert decode(data, fallback) == (text, codecs.lookup(fallback).name)
    assert extract(data, markdown=False, fallback=fallback).encoding == codecs.lookup(fallback).name
    # Without the choice the same bytes are not this text (the reason detection matters).
    with pytest.raises(NotTextError) if fallback == "cp932" else _no_error():
        assert decode(data)[0] != text


def test_utf8_and_marked_text_ignore_the_fallback() -> None:
    assert decode(WORDS.encode(), "cp1251") == (WORDS, "utf-8")
    assert decode(b"\xef\xbb\xbf" + WORDS.encode(), "cp1251") == (WORDS, "utf-8")


@pytest.mark.parametrize(
    "fallback", ["utf-16", "latin-1", "iso-8859-1", "CP1252", "cp1252 ", "cp1252\n", "", "koi8_r"]
)
def test_only_a_windows_code_page_is_a_fallback(fallback: str) -> None:
    with pytest.raises(ValueError, match="FALLBACK_ENCODINGS"):
        decode(b"text", fallback)


def test_every_fallback_is_a_python_codec() -> None:
    assert FALLBACK_ENCODING in FALLBACK_ENCODINGS
    for fallback in FALLBACK_ENCODINGS:
        assert bytes(range(32, 127)).decode(fallback) == bytes(range(32, 127)).decode("ascii")


def test_a_file_that_mixes_encodings_is_read_as_windows_1252_as_a_whole() -> None:
    text, used = decode(WORDS.encode() + b" caf\xe9")

    assert used == "cp1252"
    assert text.endswith(" café")


@pytest.mark.parametrize("data", [b"\x81", b"ok \x8d ok", b"\x90", b"\x9d", b"\x8f"])
def test_bytes_undefined_in_windows_1252_are_refused(data: bytes) -> None:
    with pytest.raises(NotTextError):
        decode(data)


@pytest.mark.parametrize(
    "data",
    [
        b"\xef\xbb\xbf" + b"caf\xe9",  # the mark says UTF-8, the bytes are not
        b"\xff\xfe" + "ab".encode("utf-16-le") + b"\x00",  # UTF-16 cut in a character
        b"\xff\xfe\x00\x00" + b"\x00\x00\x11\x00",  # UTF-32 beyond the last code point
        b"\xfe\xff\xdc\x00",  # a lone low surrogate in UTF-16
    ],
)
def test_bytes_not_valid_in_the_encoding_their_mark_names_are_refused(data: bytes) -> None:
    with pytest.raises(NotTextError):
        decode(data)


def test_a_file_that_turns_binary_after_its_head_is_refused() -> None:
    # Validation reads only the head; the extractor judges the whole file.
    head = b"plain text\n" * 1000
    assert sniff(head[:8192], complete=False) is Kind.TEXT

    with pytest.raises(NotTextError):
        decode(head + b"\x00")
    with pytest.raises(NotTextError):
        decode(head + b"\x01\x02\x03" * 4000)


@pytest.mark.parametrize(("bom", "encoding"), TEXT_BOMS)
def test_a_nul_is_refused_in_every_encoding(bom: bytes, encoding: str) -> None:
    with pytest.raises(NotTextError):
        decode(bom + "a\x00b".encode(encoding))


@pytest.mark.parametrize(
    "data",
    [
        b"\xef\xbb\xbf" + b"caf\xe9",
        b"\xff\xfe" + "ok".encode("utf-16-le") + b"\x00",
        b"\xff\xfe" + "a\x00b".encode("utf-16-le"),
        b"\xfe\xff" + "x\x01\x02\x03y".encode("utf-16-be"),
        b"\xef\xbb\xbf" + WORDS.encode(),
        b"\xff\xfe\x00\x00" + WORDS.encode("utf-32-le"),
        b"\xfe\xff" + WORDS.encode("utf-16-be"),
    ],
)
def test_marked_text_is_judged_by_the_same_rule_as_the_validator(data: bytes) -> None:
    accepted = sniff(data, complete=True) is Kind.TEXT
    try:
        decode(data)
    except NotTextError:
        decoded = False
    else:
        decoded = True

    assert decoded == accepted


def test_a_few_control_characters_are_text() -> None:
    assert decode(b"form\x0cfeed and a bell\x07 in a long enough line of text") is not None


@pytest.mark.parametrize("newline", [b"\r\n", b"\r", b"\n"])
def test_line_breaks_are_stored_as_newlines(newline: bytes) -> None:
    text, _ = decode(newline.join([b"one", b"two", b""]))

    assert text == "one\ntwo\n"


def test_utf16_line_breaks_are_converted_too() -> None:
    text, _ = decode(b"\xff\xfe" + "a\r\nb\rc".encode("utf-16-le"))

    assert text == "a\nb\nc"


def test_an_empty_file_is_one_empty_page() -> None:
    result = extract(b"", markdown=True)

    assert [(page.number, page.text, page.headings) for page in result.pages] == [(1, "", ())]


def test_the_error_names_nothing_from_the_file() -> None:
    with pytest.raises(ExtractionError) as caught:
        decode(CANARY.encode() + b"\x00")

    assert CANARY not in str(caught.value)
    assert str(caught.value) == "The file does not decode to text."


# The result


def test_a_text_file_is_one_page_without_headings() -> None:
    result = extract(b"# Not a heading in a .txt file\r\nbody\r\n", markdown=False)

    assert result.extractor == VERSION == "text-1"
    assert result.encoding == "utf-8"
    assert len(result.pages) == 1
    assert result.pages[0].number == 1
    assert result.pages[0].text == "# Not a heading in a .txt file\nbody\n"
    assert result.pages[0].headings == ()


def test_markdown_heading_offsets_point_at_the_stored_text() -> None:
    data = b"\xef\xbb\xbf# One\r\nbody\r\n\r\nTwo\r\n===\r\n## Three ##\r\n"
    page = extract(data, markdown=True).pages[0]

    assert [page.text[h.start : h.end] for h in page.headings] == [
        "# One",
        "Two\n===",
        "## Three ##",
    ]
    assert [(h.level, h.title) for h in page.headings] == [(1, "One"), (1, "Two"), (2, "Three")]


# Markdown headings


def test_atx_headings_of_every_level() -> None:
    text = "\n".join(f"{'#' * level} Level {level}" for level in range(1, 7))

    assert titles(text) == [(level, f"Level {level}") for level in range(1, 7)]


@pytest.mark.parametrize(
    "line",
    [
        "####### Seven",
        "#hashtag",
        "    # indented code",
        "\t# indented code",
        "\\# escaped",
    ],
)
def test_lines_that_are_not_atx_headings(line: str) -> None:
    assert titles(line) == []


@pytest.mark.parametrize(
    ("line", "title"),
    [
        ("# Title", "Title"),
        ("   # Three spaces in", "Three spaces in"),
        ("#\tTab", "Tab"),
        ("# Closing ###", "Closing"),
        ("# Closing ###   ", "Closing"),
        ("# Not closing#", "Not closing#"),
        ("# C# notes", "C# notes"),
        ("# Escaped \\#", "Escaped \\#"),
        ("#", ""),
        ("# #", ""),
        ("### ###", ""),
        ("# # #", "#"),
        ("# ##", ""),
        ("# Title # #", "Title #"),
        ("#   spaced   ", "spaced"),
    ],
)
def test_atx_heading_titles(line: str, title: str) -> None:
    assert titles(line) == [(1 if not line.lstrip().startswith("###") else 3, title)]


def test_setext_headings_take_the_whole_paragraph() -> None:
    text = "First line\n  second line\n===\n\nSub\n---\n\nUnderline -\n- - -\n"

    assert titles(text) == [(1, "First line second line"), (2, "Sub")]


def test_an_underline_without_a_paragraph_is_not_a_heading() -> None:
    assert titles("===\n\n---\n# After") == [(1, "After")]


def test_an_underline_after_a_list_or_a_quote_is_a_break() -> None:
    assert titles("- item\nlazy line\n---\n\n> quote\nlazy\n---\n\n1. one\n===\n") == []


def test_headings_inside_list_items_and_quotes_are_not_recorded() -> None:
    text = "- item\n\n  Inner\n  -----\n\n  # Inner too\n\n> # Quoted\n\n# Outer\n"

    assert titles(text) == [(1, "Outer")]


@pytest.mark.parametrize(
    "opening",
    [
        "> # Quoted",
        ">",
        "> ---",
        "> ```",
        ">     indented code",
        "> > # Nested",
        "> - # In a list",
        "- # Item heading",
        "-",
        "- ```",
        "- > # Quoted in a list",
    ],
)
def test_a_quote_or_list_item_that_holds_no_paragraph_takes_no_lazy_line(opening: str) -> None:
    # Only a paragraph continues lazily, so the next line starts a top-level paragraph.
    assert titles(f"{opening}\nOutside\n---\n") == [(2, "Outside")]


@pytest.mark.parametrize(
    "opening",
    ["> text", "> > text", "> - text", "- text", "- > text", "1. text", "> ``` bad`info"],
)
def test_a_quote_or_list_item_that_holds_a_paragraph_takes_lazy_lines(opening: str) -> None:
    assert titles(f"{opening}\nlazy\n===\n") == []


def test_a_list_items_inner_paragraph_takes_lazy_lines_and_its_inner_heading_does_not() -> None:
    assert titles("- a\n\n  para\nlazy\n===\n") == []
    assert titles("- a\n\n  # Inner\nOutside\n---\n") == [(2, "Outside")]


def test_a_numbered_line_that_cannot_start_a_list_stays_in_the_paragraph() -> None:
    assert titles("The year was\n1986. A good year\n---\n") == [
        (2, "The year was 1986. A good year")
    ]


def test_an_atx_heading_ends_a_list_item_and_a_paragraph() -> None:
    assert titles("- item\n# One\nparagraph\n# Two\n") == [(1, "One"), (1, "Two")]


@pytest.mark.parametrize(
    "text",
    [
        "```\n# no\n```\n# yes",
        "~~~\n# no\n~~~\n# yes",
        "````\n# no\n```\n# no\n````\n# yes",
        "~~~~\n# no\n~~~\n# no\n~~~~~\n# yes",
        "```python\n# a comment\n```\n# yes",
        "  ```\n# no\n   ```\n# yes",
        "```\n# no\n```   \n# yes",
    ],
)
def test_fenced_code_is_not_searched_for_headings(text: str) -> None:
    assert titles(text) == [(1, "yes")]


@pytest.mark.parametrize(
    "text",
    [
        "```\n# no\n~~~\n# no",  # the other character does not close it
        "```\n# no\n``` text\n# no",  # a closing fence holds nothing else
        "```\n# no\n    ```\n# no",  # four spaces in is not a fence
        "```\n# no",  # open to the end
    ],
)
def test_a_fence_stays_open_until_a_matching_closing_fence(text: str) -> None:
    assert titles(text) == []


def test_a_backtick_line_with_a_backtick_in_its_info_is_not_a_fence() -> None:
    assert titles("``` not `a fence`\n# yes") == [(1, "yes")]


@pytest.mark.parametrize("close", ["---", "...", "---   "])
def test_front_matter_is_skipped(close: str) -> None:
    text = f"---\ntitle: Notes\ntags: [a]\n{close}\n# Body\n"

    assert titles(text) == [(1, "Body")]


def test_unclosed_front_matter_is_read_as_markdown() -> None:
    assert titles("---\ntitle: Notes\n# Body") == [(1, "Body")]


def test_a_rule_that_is_not_on_the_first_line_is_not_front_matter() -> None:
    assert titles("\n---\nNotes\n---\n") == [(2, "Notes")]


def test_heading_objects() -> None:
    assert markdown_headings("ab\n## Cd\n") == (Heading(2, "Cd", 3, 8),)


def test_an_empty_list_item_cannot_interrupt_a_paragraph() -> None:
    assert titles("para\n*\n===\n") == [(1, "para *")]


@pytest.mark.parametrize(
    "text",
    [
        "-      code in the item\n  Inner\n  ---\n",  # more than four spaces: content one column in
        "-\tTab item\n\n    # in the item\n",  # a tab after the marker reaches column 4
        "-\n  Inner\n  ---\n",  # an empty item: content one column after the marker
        "10. Ten\n\n    # in the item\n",  # a longer marker moves the content column
    ],
)
def test_lines_at_a_list_items_content_column_belong_to_it(text: str) -> None:
    assert titles(text) == []


def test_a_line_left_of_the_content_column_leaves_the_list_item() -> None:
    assert titles("10. Ten\n\n   # out\n") == [(1, "out")]


@pytest.mark.parametrize(
    "text",
    [
        "10. item\n\n    paragraph\nlazy\n---\n",  # content column 4: the paragraph is not code
        "100. item\n\n     paragraph\nlazy\n---\n",
        "-    item\n\n     paragraph\nlazy\n---\n",  # four spaces after the marker
        "-\titem\n\n    paragraph\nlazy\n---\n",  # a tab after the marker
        "- a\n\n  - b\n\n    paragraph\nlazy\n---\n",  # nested items
        "1. a\n\n   10. b\n\n       paragraph\nlazy\n---\n",
        "- a\n\n  1. b\n\n     para\n\n  more of a\nlazy\n---\n",  # back out to the outer item
        "- a\n      deeper\nlazy\n---\n",  # indented code cannot interrupt the paragraph
    ],
)
def test_a_list_items_paragraph_is_judged_by_its_indent_within_the_item(text: str) -> None:
    assert titles(text) == []  # the paragraph takes "lazy", so "---" is a break


@pytest.mark.parametrize(
    "text",
    [
        "- a\n\n      code in the item\nOutside\n---\n",  # code four columns into the item
        "- a\n\n  ```\n  code\nOutside\n---\n",  # a fence in the item ends with the item
        "- a\n\n  ```\n  code\n  ```\nOutside\n---\n",
        "10. a\n\n        code\nOutside\n---\n",
    ],
)
def test_code_in_a_list_item_takes_no_lazy_line(text: str) -> None:
    assert titles(text) == [(2, "Outside")]


def test_fenced_code_in_a_list_item_is_not_read_as_paragraph_text() -> None:
    assert titles("- a\n\n  ```\n  text\n  ```\nOutside\n---\n# After\n") == [
        (2, "Outside"),
        (1, "After"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "- a\n10. b\n  ```\n  # in code\n",  # a new list starts here even at 10
        "> q\n>     code\ntext\n===\n",  # indented code cannot interrupt the quote's paragraph
    ],
)
def test_lazy_lines_keep_their_container_open(text: str) -> None:
    assert titles(text) == []


def test_a_lazy_line_keeps_the_list_item_open() -> None:
    # "text" continues the item's paragraph, so "  ```" opens a fence in the item, which ends
    # with the item at "# H" instead of swallowing it as a top-level fence would.
    assert titles("- a\ntext\n  ```\n# H\n") == [(1, "H")]


def test_fenced_code_in_a_nested_item_ends_when_a_line_backs_out_of_that_item() -> None:
    # "paragraph" is in the outer item, so the fence of the nested item has ended and
    # "outside" is the outer item's lazy line, which an underline cannot close (CommonMark).
    assert titles("- outer\n  - nested\n    ```\n    code\n  paragraph\noutside\n---\n") == []
    assert titles("- outer\n  - nested\n    ```\n  # Outer item\n# Top\n") == [(1, "Top")]


def test_a_lazy_line_left_of_a_nested_item_keeps_every_item_open() -> None:
    # "para" continues the nested item's paragraph, so the fence that follows opens in that
    # item (two spaces past its content column) and ends with it at "outside".
    assert titles("- a\n  - b\n  para\n      ```\noutside\n---\n") == [(2, "outside")]


def test_an_empty_list_item_followed_by_a_blank_line_ends() -> None:
    assert titles("-\n\n  ```\n# in code\n```\n# After\n") == [(1, "After")]


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("[ref]: /url\nTitle\n---\n", "Title"),
        ("[a]: /u 'title'\n[b]: </x y>\nTitle\n===\n", "Title"),
        ("[a]: /u\n'title on the next line'\nTitle\n---\n", "Title"),
        ("[a]:\n/destination-on-the-next-line\nTitle\n---\n", "Title"),
        ("[a]: /u 'not a title' trailing\n---\n", "[a]: /u 'not a title' trailing"),
        ("[ ]: /u\n---\n", "[ ]: /u"),  # a blank label is not one either
    ],
)
def test_link_reference_definitions_are_not_part_of_a_setext_title(text: str, title: str) -> None:
    assert titles(text) == [(2 if text.rstrip().endswith("-") else 1, title)]


@pytest.mark.parametrize(
    "text",
    [
        # CommonMark: no destination, so "[c]:" is a paragraph and "===" underlines it (level 1).
        # markdown-it-py reads "===" on the next line as the definition's destination.
        "[c]:\n===\n",
        # CommonMark: "-" continues the paragraph (an empty item cannot interrupt one), the
        # definition is taken out when it closes, and "---" underlines "-" (level 2).
        # markdown-it-py takes the definition out first, so "-" is an empty list item.
        "[ref]: /url\n-\n---\n",
    ],
)
def test_known_deviations_from_commonmark_on_link_reference_definitions(text: str) -> None:
    # markdown-it-py departs from the specification here (ADR 0018); the reference implementation
    # finds a heading in each. These tests record the library's behaviour, so an upgrade that
    # changes it is noticed.
    assert titles(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "<div>\n# Inside\n</div>\n\n# Real\n",  # an HTML block runs to its blank line
        "<!--\n# Inside\n-->\n# Real\n",  # a comment runs to its closing marker
    ],
)
def test_headings_inside_an_html_block_are_not_recorded(text: str) -> None:
    assert titles(text) == [(1, "Real")]


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("[ref]: </x y>\nTitle\n---\n", "Title"),  # a pointy destination may hold a space
        ("[ref]: /a(b)\nTitle\n---\n", "Title"),
        ("[ref]: /a\\(b\nTitle\n---\n", "Title"),  # an escaped parenthesis needs no partner
        ("[ref]: /a(b\nTitle\n---\n", "[ref]: /a(b Title"),  # unbalanced: not a definition
        ("[ref]: /a)b\nTitle\n---\n", "[ref]: /a)b Title"),
    ],
)
def test_a_bare_destination_needs_balanced_parentheses(text: str, title: str) -> None:
    assert titles(text) == [(2, title)]


@pytest.mark.parametrize("marker", ["-", "*", "1.", "10)"])
def test_a_list_item_that_opens_with_indented_code_takes_no_lazy_line(marker: str) -> None:
    # Five spaces after the marker: one of padding, then four of code indent (list items rule 2).
    assert titles(f"{marker}     code\nOutside\n---\n") == [(2, "Outside")]
    assert titles(f"{marker}    text\nlazy\n---\n") == []  # four spaces: a paragraph


def test_a_heading_after_definitions_starts_at_its_own_text() -> None:
    [heading] = markdown_headings("[ref]: /url\nTitle\n---\n")
    assert heading == Heading(2, "Title", 12, 21)


@pytest.mark.parametrize("text", ["[ref]: /url\n===\n", "[ref]: /url\n---\n"])
def test_an_underline_under_definitions_alone_is_no_heading(text: str) -> None:
    assert titles(text) == []  # "===" is paragraph text; "---" a thematic break


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("> ~~~\n> code\nOutside\n---\n", "Outside"),  # fenced code in a quote takes no lazy line
        ("> - a\n>   ```\n>   x\nOut\n---\n", "Out"),  # nor in a list item in a quote
        ("- > ```\n  > x\nOut\n---\n", "Out"),  # nor in a quote in a list item
    ],
)
def test_fenced_code_in_a_container_ends_with_it(text: str, title: str) -> None:
    assert titles(text) == [(2, title)]


def test_a_lazy_line_after_a_quote_is_no_setext_heading() -> None:
    # CommonMark: "Outside" continues the quote's paragraph lazily, and "---" cannot, so it is a
    # thematic break (the reference implementation agrees).
    assert titles("> quoted\nOutside\n---\n") == []


def test_every_text_and_markdown_file_of_the_demo_corpus_extracts() -> None:
    corpus = Path(__file__).resolve().parents[3] / "demo" / "corpus"
    files = sorted(p for p in corpus.rglob("*") if p.suffix in {".txt", ".md"})
    assert files

    for path in files:
        result = extract(path.read_bytes(), markdown=path.suffix == ".md")
        page = result.pages[0]
        assert result.encoding == "utf-8"
        assert page.text
        assert all(page.text[h.start : h.end].strip() for h in page.headings)
        assert (path.suffix == ".md") == bool(page.headings)
