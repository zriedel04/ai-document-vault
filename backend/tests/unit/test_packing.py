import logging
import random
from fractions import Fraction

import pytest

from vault.core.config import RetrievalSettings
from vault.db.pages import PageText
from vault.ingest.chunking import structural_v1
from vault.retrieval.fusion import FusedHit
from vault.retrieval.packing import (
    SEPARATOR,
    SOURCE_ALLOWANCE_TOKENS,
    ContextChunk,
    PackedContext,
    PackingError,
    estimate_tokens,
    pack,
)

# Synthetic ids (ULIDs) and titles.
DOC_A = "01J00000000000000000000000"
DOC_B = "01J00000000000000000000001"
VER_A = "01J000000000000000000000A0"
VER_A2 = "01J000000000000000000000A1"
VER_B = "01J000000000000000000000B0"
TITLES = {DOC_A: "Synthetic notes.txt", DOC_B: "Made up report.pdf"}
PRIVATE_WORD = "Zebracorn"  # a word that must never reach a log line or an error


def settings(budget: int = 8000, neighbors: int = 0) -> RetrievalSettings:
    return RetrievalSettings(context_budget_tokens=budget, context_neighbor_chunks=neighbors)


def words(count: int, seed: int = 0) -> str:
    rng = random.Random(seed)  # noqa: S311 (test data, not a secret)
    return " ".join(
        "".join(rng.choice("abcdefgh") for _ in range(rng.randint(2, 7))) for _ in range(count)
    )


def chunk(
    chunk_id: int,
    index: int,
    text: str,
    *,
    page: int = 1,
    start: int = 0,
    section: tuple[str, ...] = ("Intro",),
    document: str = DOC_A,
    version: str = VER_A,
) -> ContextChunk:
    return ContextChunk(
        chunk_id=chunk_id,
        document_id=document,
        version_id=version,
        index=index,
        page_start=page,
        page_end=page,
        char_start=start,
        char_end=start + len(text),
        section_path=section,
        text=text,
    )


def hit(chunk_id: int, document: str = DOC_A, rank: int = 1) -> FusedHit:
    return FusedHit(chunk_id, document, Fraction(1, 60 + rank), rank, None)


def by_id(*chunks: ContextChunk) -> dict[int, ContextChunk]:
    return {c.chunk_id: c for c in chunks}


def source_cost(text: str, title: str, section: tuple[str, ...]) -> int:
    return (
        SOURCE_ALLOWANCE_TOKENS
        + estimate_tokens(title)
        + sum(estimate_tokens(t) for t in section)
        + estimate_tokens(text)
    )


# The estimate.


def test_the_estimate_by_hand() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("   \t ") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("abcdef ghi") == 3
    assert estimate_tokens("CSE485") == 2
    assert estimate_tokens("a.b") == 3
    assert estimate_tokens("one\n\ntwo") == 4  # each line break is a token
    assert estimate_tokens("été") == 3  # accented letters count one each
    assert estimate_tokens("文書の") == 3  # one per CJK character
    assert estimate_tokens("a_b") == 3


def _random_text(rng: random.Random) -> str:
    alphabet = "abcXYZ019_ .,;!?\n\téß文書。́\U0001f600-'\"()"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))


def test_the_estimate_is_between_the_chunker_count_and_the_utf8_bytes() -> None:
    rng = random.Random(485)  # noqa: S311 (test data, not a secret)
    for _ in range(3000):
        text = _random_text(rng)
        estimate = estimate_tokens(text)
        assert structural_v1.count_tokens(text) <= estimate <= len(text.encode("utf-8"))


def test_the_estimate_is_high_for_english() -> None:
    text = (
        "The prototype is due on the fifteenth of October, and the final report follows two "
        "weeks later. Each team presents its design to the sponsor before the deadline."
    )
    # Sub-word tokenizers use about four characters of English per token.
    assert estimate_tokens(text) >= len(text) / 4
    assert estimate_tokens(text) <= len(text) / 2


# Empty input and the budget.


def test_empty_input_packs_nothing() -> None:
    context = pack([], {}, {}, settings())
    assert context == PackedContext((), 8000, 0, 0, 0, 0)
    assert context.source("S1") is None


def test_a_source_that_fills_the_budget_exactly_fits_and_one_token_more_does_not() -> None:
    text = words(700)
    cost = source_cost(text, TITLES[DOC_A], ("Intro",))
    assert 1000 <= cost <= 200000
    chunks = by_id(chunk(1, 0, text))
    exact = pack([hit(1)], chunks, TITLES, settings(budget=cost))
    assert [s.label for s in exact.sources] == ["S1"]
    assert exact.estimated_tokens == cost == exact.sources[0].estimated_tokens
    assert (exact.hits_packed, exact.hits_left_out) == (1, 0)
    under = pack([hit(1)], chunks, TITLES, settings(budget=cost - 1))
    assert under.sources == ()
    assert (under.hits_packed, under.hits_left_out) == (0, 1)


def test_the_sum_of_sources_is_held_to_the_budget() -> None:
    first, second = words(400, 1), words(400, 2)
    cost_1 = source_cost(first, TITLES[DOC_A], ("Intro",))
    cost_2 = source_cost(second, TITLES[DOC_B], ())
    chunks = by_id(
        chunk(1, 0, first), chunk(2, 0, second, section=(), document=DOC_B, version=VER_B)
    )
    ranking = [hit(1), hit(2, DOC_B, 2)]
    both = pack(ranking, chunks, TITLES, settings(budget=cost_1 + cost_2))
    assert [s.document_id for s in both.sources] == [DOC_A, DOC_B]
    assert both.estimated_tokens == cost_1 + cost_2
    one = pack(ranking, chunks, TITLES, settings(budget=cost_1 + cost_2 - 1))
    assert [s.document_id for s in one.sources] == [DOC_A]
    assert one.hits_left_out == 1


def test_one_chunk_larger_than_the_budget_is_left_out_and_smaller_ones_still_fit() -> None:
    huge, small = words(3000, 3), words(100, 4)
    chunks = by_id(chunk(1, 0, huge), chunk(2, 0, small, section=(), document=DOC_B, version=VER_B))
    context = pack([hit(1), hit(2, DOC_B, 2)], chunks, TITLES, settings(budget=1000))
    assert estimate_tokens(huge) > 1000
    assert [s.label for s in context.sources] == ["S1"]
    assert context.sources[0].document_id == DOC_B
    assert context.sources[0].text == small
    assert (context.hits_packed, context.hits_left_out) == (1, 1)
    assert context.estimated_tokens <= 1000


def test_a_merge_that_would_overrun_the_budget_leaves_the_hit_out() -> None:
    first, second = words(700, 5), words(700, 6)
    chunks = by_id(chunk(1, 0, first), chunk(2, 1, second, start=len(first) + 2))
    budget = source_cost(first, TITLES[DOC_A], ("Intro",)) + 10
    context = pack([hit(1), hit(2, rank=2)], chunks, TITLES, settings(budget=budget))
    assert [s.text for s in context.sources] == [first]
    assert context.hits_left_out == 1


# Merging adjacent chunks.


def test_adjacent_chunks_across_pages_merge_into_one_source() -> None:
    one, two = "End of the first page.", "Start of the second page."
    chunks = by_id(chunk(1, 0, one, page=3, start=10), chunk(2, 1, two, page=4, start=0))
    context = pack([hit(2), hit(1, rank=2)], chunks, TITLES, settings())
    (source,) = context.sources
    assert (source.page_start, source.page_end) == (3, 4)
    assert source.text == one + SEPARATOR + two
    assert source.section_path == ("Intro",)
    assert source.title == TITLES[DOC_A]
    assert [(p.chunk_id, p.page_start, p.char_start, p.char_end) for p in source.parts] == [
        (1, 3, 10, 10 + len(one)),
        (2, 4, 0, len(two)),
    ]
    for part, text in zip(source.parts, (one, two), strict=True):
        assert source.text[part.source_start : part.source_end] == text
    assert source.parts[0].hit == hit(1, rank=2)
    assert source.parts[1].hit == hit(2)
    assert context.hits_packed == 2


def test_overlapping_chunks_on_one_page_do_not_repeat_text() -> None:
    page = "Alpha one. Beta two. Gamma three. Delta four."
    first = page[: page.index(" Gamma")]  # "Alpha one. Beta two."
    second_start = page.index("Beta")
    second = page[second_start:]  # starts again at the last sentence of the first
    chunks = by_id(chunk(1, 0, first), chunk(2, 1, second, start=second_start))
    (source,) = pack([hit(1), hit(2, rank=2)], chunks, TITLES, settings()).sources
    assert source.text == page
    assert source.text.count("Beta two.") == 1
    part = source.parts[1]
    assert part.chunk_start == len(first) - second_start
    assert part.char_start == len(first)
    assert source.text[part.source_start : part.source_end] == second[part.chunk_start :]
    assert page[part.char_start : part.char_end] == second[part.chunk_start :]


def test_overlap_with_different_text_is_refused() -> None:
    chunks = by_id(chunk(1, 0, "Alpha one. Beta two."), chunk(2, 1, "Bxta two. More.", start=11))
    with pytest.raises(PackingError, match="overlap"):
        pack([hit(1), hit(2, rank=2)], chunks, TITLES, settings())


def test_offsets_on_another_page_are_not_an_overlap() -> None:
    # Page 2 starts at 0, before the end of chunk 1 on page 1: still joined, nothing cut.
    chunks = by_id(chunk(1, 0, "Page one text.", page=1, start=5), chunk(2, 1, "Two.", page=2))
    (source,) = pack([hit(1), hit(2, rank=2)], chunks, TITLES, settings()).sources
    assert source.text == "Page one text." + SEPARATOR + "Two."


@pytest.mark.parametrize(
    "second",
    [
        chunk(2, 1, "Back a page.", page=1),
        chunk(2, 1, "Earlier.", page=2, start=0),  # starts before the first, same page
        chunk(2, 1, "irst.", page=2, start=21),  # inside the first, adds nothing
    ],
)
def test_consecutive_chunks_out_of_reading_order_are_refused(second: ContextChunk) -> None:
    chunks = by_id(chunk(1, 0, "Text of the first.", page=2, start=8), second)
    with pytest.raises(PackingError):
        pack([hit(1), hit(2, rank=2)], chunks, TITLES, settings())


def test_chunks_that_are_not_adjacent_stay_apart() -> None:
    chunks = by_id(
        chunk(1, 0, "Zero."),
        chunk(2, 2, "Two.", start=20),  # a gap in the indexes
        chunk(3, 3, "Three.", start=30, section=("Other",)),  # another section
        chunk(4, 1, "One again.", version=VER_A2),  # another version
    )
    ranking = [hit(1), hit(2, rank=2), hit(3, rank=3), hit(4, rank=4)]
    context = pack(ranking, chunks, TITLES, settings())
    assert [s.text for s in context.sources] == ["Zero.", "Two.", "Three.", "One again."]
    assert [s.label for s in context.sources] == ["S1", "S2", "S3", "S4"]


def test_a_hit_that_closes_a_gap_merges_two_sources_at_the_better_place() -> None:
    chunks = by_id(
        chunk(10, 0, "Other doc.", document=DOC_B, version=VER_B),
        chunk(3, 3, "Three.", start=0),
        chunk(5, 5, "Five.", start=20),
        chunk(4, 4, "Four.", start=10),
    )
    ranking = [hit(5), hit(10, DOC_B, 2), hit(3, rank=3), hit(4, rank=4)]
    context = pack(ranking, chunks, TITLES, settings())
    assert [s.text for s in context.sources] == [
        SEPARATOR.join(["Three.", "Four.", "Five."]),
        "Other doc.",
    ]
    assert [p.chunk_id for p in context.sources[0].parts] == [3, 4, 5]


# Neighbours.


def test_neighbours_are_added_before_and_after_when_the_budget_allows() -> None:
    chunks = by_id(
        chunk(1, 0, "Before before."),
        chunk(2, 1, "Before.", start=20),
        chunk(3, 2, "The hit.", start=30),
        chunk(4, 3, "After.", start=40),
        chunk(5, 4, "After after.", start=50),
    )
    none = pack([hit(3)], chunks, TITLES, settings(neighbors=0))
    assert [s.text for s in none.sources] == ["The hit."]
    one = pack([hit(3)], chunks, TITLES, settings(neighbors=1))
    (source,) = one.sources
    assert source.text == SEPARATOR.join(["Before.", "The hit.", "After."])
    assert [p.hit is None for p in source.parts] == [True, False, True]
    assert (one.hits_packed, one.neighbors_added) == (1, 2)
    two = pack([hit(3)], chunks, TITLES, settings(neighbors=2))
    assert two.sources[0].text.startswith("Before before.")
    assert two.neighbors_added == 4


def test_neighbours_stay_in_the_section_and_never_displace_a_hit() -> None:
    hit_text, other = words(450, 7), words(450, 8)
    chunks = by_id(
        chunk(1, 0, "Heading before.", section=("Earlier",)),
        chunk(2, 1, hit_text, start=20),
        chunk(3, 2, words(450, 9), start=20 + len(hit_text) + 2),
        chunk(9, 0, other, section=(), document=DOC_B, version=VER_B),
    )
    budget = source_cost(hit_text, TITLES[DOC_A], ("Intro",)) + source_cost(
        other, TITLES[DOC_B], ()
    )
    context = pack([hit(2), hit(9, DOC_B, 2)], chunks, TITLES, settings(budget, neighbors=1))
    # Chunk 1 is in another section and chunk 3 does not fit next to both hits.
    assert [s.text for s in context.sources] == [hit_text, other]
    assert context.neighbors_added == 0


def test_a_neighbour_in_another_section_is_not_added_even_with_room() -> None:
    chunks = by_id(
        chunk(1, 0, "Under the first heading.", section=("First",)),
        chunk(2, 1, "The hit.", start=30, section=("Second",)),
        chunk(3, 2, "Under a sub-heading.", start=40, section=("Second", "Sub")),
    )
    context = pack([hit(2)], chunks, TITLES, settings(neighbors=3))
    assert [s.text for s in context.sources] == ["The hit."]
    assert context.neighbors_added == 0


def test_a_neighbour_may_join_two_sources() -> None:
    chunks = by_id(
        chunk(1, 0, "Zero."), chunk(2, 1, "One.", start=10), chunk(3, 2, "Two.", start=20)
    )
    context = pack([hit(3), hit(1, rank=2)], chunks, TITLES, settings(neighbors=1))
    (source,) = context.sources
    assert source.text == SEPARATOR.join(["Zero.", "One.", "Two."])
    assert [p.hit is not None for p in source.parts] == [True, False, True]
    assert (context.hits_packed, context.neighbors_added) == (2, 1)


# Order and labels.


def test_the_order_follows_the_ranking_and_not_the_mappings() -> None:
    items = [
        chunk(i + 1, 0, f"Text {i}.", document=doc, version=ver)
        for i, (doc, ver) in enumerate(
            [(DOC_A, VER_A), (DOC_B, VER_B), (DOC_A, VER_A2)],
        )
    ]
    ranking = [hit(3), hit(1, rank=2), hit(2, DOC_B, 3)]
    expected = pack(ranking, by_id(*items), TITLES, settings(neighbors=1))
    assert [s.text for s in expected.sources] == ["Text 2.", "Text 0.", "Text 1."]
    rng = random.Random(7)  # noqa: S311 (test data, not a secret)
    for _ in range(20):
        shuffled = items[:]
        rng.shuffle(shuffled)
        titles = dict(reversed(list(TITLES.items())))
        assert pack(ranking, by_id(*shuffled), titles, settings(neighbors=1)) == expected


def test_labels_and_lookup() -> None:
    chunks = by_id(chunk(1, 0, "One."), chunk(2, 0, "Two.", document=DOC_B, version=VER_B))
    context = pack([hit(2, DOC_B), hit(1, rank=2)], chunks, TITLES, settings())
    s1, s2 = context.sources
    assert (s1.label, s1.title, s1.version_id) == ("S1", TITLES[DOC_B], VER_B)
    assert context.source("S2") is s2
    assert context.source("S3") is None
    assert context.source("s1") is None


def test_a_span_maps_back_to_its_chunks() -> None:
    chunks = by_id(chunk(1, 0, "First chunk.", page=1), chunk(2, 1, "Second chunk.", page=2))
    (source,) = pack([hit(1), hit(2, rank=2)], chunks, TITLES, settings()).sources
    quote = source.text.index("chunk.")
    assert [p.chunk_id for p in source.parts_for(quote, quote + 6)] == [1]
    across = source.text.index("chunk.\n\nSecond")
    assert [p.chunk_id for p in source.parts_for(across, across + 14)] == [1, 2]
    for bad in ((0, 0), (-1, 3), (0, len(source.text) + 1), (True, 3)):
        with pytest.raises(PackingError):
            source.parts_for(*bad)


def test_the_real_chunker_s_runs_pack_back_into_the_page_text() -> None:
    sentences = [f"Sentence {i} says {words(6, i)}." for i in range(400)]
    pages = [
        PageText(1, " ".join(sentences[:200])),
        PageText(2, " ".join(sentences[200:])),
    ]
    made = structural_v1.chunk_pages(pages)
    assert len(made) >= 4
    chunks = {
        i + 1: ContextChunk(
            chunk_id=i + 1,
            document_id=DOC_A,
            version_id=VER_A,
            index=c.index,
            page_start=c.page_start,
            page_end=c.page_end,
            char_start=c.char_start,
            char_end=c.char_end,
            section_path=c.section_path,
            text=c.text,
        )
        for i, c in enumerate(made)
    }
    ranking = [hit(i, rank=i) for i in reversed(range(1, len(made) + 1))]
    (source,) = pack(ranking, chunks, TITLES, settings(budget=200000)).sources
    page_texts = {page.number: page.text for page in pages}
    # Within a page the source is exactly the page's text; pages are joined by the separator.
    expected = SEPARATOR.join(
        page_texts[n][
            min(c.char_start for c in made if c.page_start == n) : max(
                c.char_end for c in made if c.page_start == n
            )
        ]
        for n in (1, 2)
    )
    assert source.text == expected
    for part in source.parts:
        assert (
            source.text[part.source_start : part.source_end]
            == page_texts[part.page_start][part.char_start : part.char_end]
        )


# Refused input, logging.


@pytest.mark.parametrize(
    ("ranking", "chunks", "titles"),
    [
        ([hit(1), hit(1)], by_id(chunk(1, 0, PRIVATE_WORD)), TITLES),
        ([hit(2)], by_id(chunk(1, 0, PRIVATE_WORD)), TITLES),
        ([hit(1, DOC_B)], by_id(chunk(1, 0, PRIVATE_WORD)), TITLES),
        ([hit(1)], by_id(chunk(1, 0, PRIVATE_WORD)), {}),
        ([hit(1)], by_id(chunk(1, 0, PRIVATE_WORD)), {DOC_A: "  "}),
        (["not a hit"], by_id(chunk(1, 0, PRIVATE_WORD)), TITLES),
        ([hit(1)], {2: chunk(1, 0, PRIVATE_WORD)}, TITLES),
        ([hit(1)], by_id(chunk(1, 0, PRIVATE_WORD), chunk(2, 0, "Same place.")), TITLES),
        (
            [hit(1)],
            by_id(chunk(1, 0, PRIVATE_WORD), chunk(2, 1, "x", document=DOC_B, start=20)),
            TITLES,
        ),
    ],
)
def test_bad_input_is_refused_with_fixed_text(
    ranking: list[FusedHit], chunks: dict[int, ContextChunk], titles: dict[str, str]
) -> None:
    with pytest.raises(PackingError) as raised:
        pack(ranking, chunks, titles, settings())
    assert PRIVATE_WORD not in str(raised.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"chunk_id": 0},
        {"chunk_id": True},
        {"index": -1},
        {"document_id": "doc"},
        {"page_start": 0},
        {"page_end": 0},
        {"char_end": 0},
        {"text": ""},
        {"text": PRIVATE_WORD + "!"},  # longer than its span
        {"section_path": ["Intro"]},
    ],
)
def test_a_chunk_that_breaks_the_rules_is_refused(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "chunk_id": 1,
        "document_id": DOC_A,
        "version_id": VER_A,
        "index": 0,
        "page_start": 1,
        "page_end": 1,
        "char_start": 0,
        "char_end": len(PRIVATE_WORD),
        "section_path": ("Intro",),
        "text": PRIVATE_WORD,
    }
    values.update(changes)
    with pytest.raises(PackingError) as raised:
        ContextChunk(**values)  # type: ignore[arg-type]
    assert PRIVATE_WORD not in str(raised.value)


def test_the_log_line_holds_counts_only(caplog: pytest.LogCaptureFixture) -> None:
    chunks = by_id(chunk(1, 0, PRIVATE_WORD, section=(PRIVATE_WORD,)))
    titles = {DOC_A: PRIVATE_WORD + ".txt"}
    with caplog.at_level(logging.DEBUG, logger="vault.retrieval.packing"):
        pack([hit(1)], chunks, titles, settings())
    (record,) = [r for r in caplog.records if r.name == "vault.retrieval.packing"]
    assert record.getMessage() == "retrieval.context_packed"
    rendered = repr(record.__dict__)
    assert PRIVATE_WORD not in rendered
    assert DOC_A not in rendered
