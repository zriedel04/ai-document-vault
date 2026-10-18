"""Chunker v1 (``structural-1``): traceability, section paths, sizes and overlap (synthetic)."""

from __future__ import annotations

import itertools
import random
from collections.abc import Sequence

import pytest

from vault.db.pages import PageHeading, PageText
from vault.ingest.chunking.base import Chunk
from vault.ingest.chunking.structural_v1 import (
    MAX_TOKENS,
    MIN_TOKENS,
    OVERLAP_TOKENS,
    chunk_pages,
    count_tokens,
    section_title,
)

# Letters outside ASCII, built from code points: accented Latin, Greek, CJK, an emoji outside the
# Basic Multilingual Plane, a combining accent and a no-break space.
CAFE = "café"
GREEK = "αβγ"
CJK = "履歴書"
EMOJI = "\U0001f600"
COMBINING = "e\u0301"
NBSP = "\u00a0"


class Seeded(random.Random):
    """A seeded generator for test data (not for anything secret)."""


def sentence(rng: random.Random, words: int = 12) -> str:
    vocabulary = ["alpha", "beta", "gamma", "delta", "report", "CSE", "485", CAFE, GREEK, "x"]
    return " ".join(rng.choice(vocabulary) for _ in range(words)) + "."


def paragraph(rng: random.Random, sentences: int = 5) -> str:
    return " ".join(sentence(rng) for _ in range(sentences))


def page_with_headings(number: int, parts: Sequence[tuple[int, str] | str]) -> PageText:
    """A page from parts: ``(level, title)`` makes a heading line, a string is a paragraph."""
    text = ""
    headings: list[PageHeading] = []
    for part in parts:
        if isinstance(part, tuple):
            level, title = part
            line = f"{'#' * level} {title}"
            headings.append(PageHeading(level, title, len(text), len(text) + len(line)))
            text += line + "\n"
        else:
            text += part + "\n\n"
    return PageText(number, text, tuple(headings))


def assert_traceable(pages: Sequence[PageText], chunks: Sequence[Chunk]) -> None:
    """Every chunk maps to a valid page and offsets, its text is that slice, and every visible
    character of every page is in at least one chunk."""
    covered = {page.number: [False] * len(page.text) for page in pages}
    for position, chunk in enumerate(chunks):
        assert chunk.index == position
        assert 1 <= chunk.page_start == chunk.page_end <= len(pages)
        text = pages[chunk.page_start - 1].text
        assert 0 <= chunk.char_start < chunk.char_end <= len(text)
        assert chunk.text == text[chunk.char_start : chunk.char_end]
        assert chunk.text == chunk.text.strip()
        assert chunk.token_count == count_tokens(chunk.text)
        assert 1 <= chunk.token_count <= MAX_TOKENS
        for offset in range(chunk.char_start, chunk.char_end):
            covered[chunk.page_start][offset] = True
    for page in pages:
        for offset, character in enumerate(page.text):
            assert covered[page.number][offset] or character.isspace(), (page.number, offset)


def assert_overlap_is_small(chunks: Sequence[Chunk]) -> None:
    for before, after in itertools.pairwise(chunks):
        if before.page_start != after.page_start or before.section_path != after.section_path:
            continue
        if after.char_start < before.char_end:
            overlap = before.text[after.char_start - before.char_start :]
            assert after.text.startswith(overlap)
            assert count_tokens(overlap) <= OVERLAP_TOKENS
            assert after.char_end > before.char_end  # every chunk brings new text


# --- traceability --------------------------------------------------------------------------------


def test_every_chunk_is_the_text_of_its_page_at_its_offsets() -> None:
    rng = Seeded(1)
    pages = [
        page_with_headings(1, ["Preface " + paragraph(rng), (1, "Notes"), paragraph(rng, 40)]),
        page_with_headings(2, [paragraph(rng, 30), (2, "Plan"), paragraph(rng, 60)]),
        PageText(3, ""),
        page_with_headings(4, [(1, "End"), paragraph(rng, 2)]),
    ]
    chunks = chunk_pages(pages)
    assert len(chunks) > 4
    assert_traceable(pages, chunks)
    assert_overlap_is_small(chunks)
    assert chunk_pages(pages) == chunks  # deterministic


def test_offsets_count_characters_not_bytes() -> None:
    text = f"{EMOJI} {CJK} {COMBINING}{NBSP}{CAFE} {GREEK}.\n\nCSE 485 {EMOJI}"
    pages = [PageText(1, f"{NBSP} {text} 　")]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    assert [chunk.text for chunk in chunks] == [text]
    assert chunks[0].char_start == 2


# --- section paths -------------------------------------------------------------------------------


def test_section_paths_follow_the_heading_levels_across_pages() -> None:
    pages = [
        page_with_headings(
            1,
            ["Before any heading.", (1, "Course"), "Intro.", (2, "Week 1"), "Read.", (3, "Lab")],
        ),
        page_with_headings(2, ["Lab text on the next page.", (2, "Week 2"), "Write."]),
        page_with_headings(3, [(1, "Appendix"), (3, "Deep"), "Skipped a level."]),
    ]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    paths = [(chunk.page_start, chunk.section_path, chunk.text.split("\n")[-1]) for chunk in chunks]
    assert paths == [
        (1, (), "Before any heading."),
        (1, ("Course",), "Intro."),
        (1, ("Course", "Week 1"), "Read."),
        (1, ("Course", "Week 1", "Lab"), "### Lab"),
        (2, ("Course", "Week 1", "Lab"), "Lab text on the next page."),
        (2, ("Course", "Week 2"), "Write."),
        (3, ("Appendix",), "# Appendix"),
        (3, ("Appendix", "Deep"), "Skipped a level."),
    ]


def test_a_heading_starts_its_sections_chunk() -> None:
    pages = [page_with_headings(1, [(1, "Title"), "Body."])]
    (chunk,) = chunk_pages(pages)
    assert chunk.text == "# Title\nBody."
    assert chunk.section_path == ("Title",)


def test_section_titles_are_kept_in_one_line_form() -> None:
    assert section_title(f" A\tB\x01 \x85 C\n{NBSP}{CAFE}  ") == f"A B C {CAFE}"
    assert section_title("") == ""
    text = "T\x01itle\nBody."
    pages = [PageText(1, text, (PageHeading(2, "T\x01itle", 0, 6),))]
    assert chunk_pages(pages)[0].section_path == ("T itle",)


def test_a_document_with_no_headings_has_no_section_path() -> None:
    rng = Seeded(2)
    pages = [PageText(1, paragraph(rng, 80)), PageText(2, paragraph(rng, 3))]
    chunks = chunk_pages(pages)
    assert len(chunks) > 2
    assert_traceable(pages, chunks)
    assert all(chunk.section_path == () for chunk in chunks)


# --- sizes ---------------------------------------------------------------------------------------


def test_tokens_are_runs_of_up_to_four_letters_or_digits_or_one_other_character() -> None:
    assert count_tokens("") == 0
    assert count_tokens("   \n") == 0
    assert count_tokens("the cat") == 2
    assert count_tokens("internationalization") == 5
    assert count_tokens("CSE 485, ok?") == 5
    assert count_tokens(f"{CJK}{CJK}") == 2
    assert count_tokens(EMOJI) == 1
    assert count_tokens("abcdef", 2, 5) == 1


def test_chunks_stay_within_the_size_and_fill_to_the_minimum() -> None:
    rng = Seeded(3)
    pages = [PageText(1, "\n\n".join(paragraph(rng, rng.randint(1, 8)) for _ in range(60)))]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    assert len(chunks) > 5
    for chunk in chunks[:-1]:
        assert MIN_TOKENS <= chunk.token_count <= MAX_TOKENS
    assert_overlap_is_small(chunks)


def test_a_chunk_prefers_to_end_at_a_paragraph() -> None:
    rng = Seeded(4)
    paragraphs = [paragraph(rng, 7) for _ in range(20)]  # about 120 tokens each
    text = "\n\n".join(paragraphs)
    pages = [PageText(1, text)]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    for chunk in chunks[:-1]:
        assert text[chunk.char_end : chunk.char_end + 2] == "\n\n"


def test_overlap_repeats_the_last_sentence_when_a_paragraph_continues() -> None:
    rng = Seeded(5)
    text = paragraph(rng, 120)  # one paragraph, much longer than a chunk
    pages = [PageText(1, text)]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    assert len(chunks) >= 3
    for before, after in itertools.pairwise(chunks):
        assert after.char_start < before.char_end
    assert_overlap_is_small(chunks)


def test_a_word_longer_than_a_chunk_is_cut_into_tokens() -> None:
    word = "abcd" * (MAX_TOKENS * 2 + 10)
    pages = [PageText(1, f"start {word} end")]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    assert [chunk.token_count for chunk in chunks] == [MAX_TOKENS, MAX_TOKENS, 13]


def test_text_with_no_spaces_is_cut_at_its_full_stops() -> None:
    sentence_cjk = CJK * 30 + "。"  # 23 tokens with the full stop
    pages = [PageText(1, sentence_cjk * 60)]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    assert len(chunks) > 1
    assert all(chunk.text.endswith("。") for chunk in chunks)


def test_a_sentence_longer_than_a_chunk_is_cut_at_white_space() -> None:
    pages = [PageText(1, " ".join(["word"] * (MAX_TOKENS * 3)))]
    chunks = chunk_pages(pages)
    assert_traceable(pages, chunks)
    assert len(chunks) >= 3


# --- empty input ---------------------------------------------------------------------------------


def test_empty_pages_give_no_chunks() -> None:
    assert chunk_pages([PageText(1, "")]) == ()
    assert chunk_pages([PageText(1, f" \n\t\n{NBSP}　"), PageText(2, "")]) == ()


def test_an_empty_page_between_others_keeps_the_page_numbers() -> None:
    pages = [PageText(1, "One."), PageText(2, "\n\n"), PageText(3, "Three.")]
    chunks = chunk_pages(pages)
    assert [(chunk.page_start, chunk.text) for chunk in chunks] == [(1, "One."), (3, "Three.")]


def test_pages_that_break_the_page_rules_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one page"):
        chunk_pages([])
    with pytest.raises(ValueError, match="Page 1"):
        chunk_pages([PageText(2, "x")])


# --- generated documents -------------------------------------------------------------------------


def generated_pages(rng: random.Random) -> list[PageText]:
    pieces = [
        "word",
        "CSE 485",
        CAFE,
        CJK,
        EMOJI,
        COMBINING,
        ".",
        "!",
        "。",
        " ",
        " ",
        " ",
        "\n",
        "\n\n",
        "\r\n",
        NBSP,
        "\t",
    ]
    result: list[PageText] = []
    for number in range(1, rng.randint(1, 4) + 1):
        text = ""
        headings: list[PageHeading] = []
        for _ in range(rng.randint(0, 400)):
            if rng.random() < 0.02:
                line = "#" + rng.choice(["Title", f"{CAFE} {CJK}", " ", "A\tB"])
                if text and not text.endswith("\n"):
                    text += "\n"
                headings.append(PageHeading(rng.randint(1, 6), line[1:], len(text), len(text) + 1))
                text += line + "\n"
            elif rng.random() < 0.01:
                text += "x" * rng.randint(1, 3000)  # up to 750 tokens: longer than a chunk
            else:
                text += rng.choice(pieces) * rng.choice([1, 1, 1, 5, 20])
        if rng.random() < 0.15:  # a page with no visible text
            text, headings = rng.choice(["", " \n\n\t", NBSP * 3, "\r\n"]), []
        result.append(PageText(number, text, tuple(headings)))
    return result


def test_generated_documents_keep_every_rule() -> None:
    rng = Seeded(485)
    seen = {"overlap": 0, "full": 0, "several_sections": 0, "empty": 0}
    for _ in range(300):
        pages = generated_pages(rng)
        chunks = chunk_pages(pages)
        assert_traceable(pages, chunks)
        assert_overlap_is_small(chunks)
        for before, after in itertools.pairwise(chunks):
            assert (before.page_start, before.char_start) < (after.page_start, after.char_start)
            same_page = before.page_start == after.page_start
            seen["overlap"] += same_page and after.char_start < before.char_end
        seen["full"] += any(chunk.token_count == MAX_TOKENS for chunk in chunks)
        seen["several_sections"] += len({chunk.section_path for chunk in chunks}) > 2
        seen["empty"] += not chunks
    # The generator builds the shapes the rules are about, so a broken rule fails this test.
    assert all(count >= 5 for count in seen.values()), seen
