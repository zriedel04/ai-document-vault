"""Citation validation (plan section 11, ADR 0011): labels, quotes, statuses and the schema."""

from __future__ import annotations

import json
import random
import unicodedata
from typing import Any

import pytest

from vault.answers import citations, graphemes
from vault.answers.citations import (
    MAX_ANSWER_CHARS,
    MAX_LABEL_CHARS,
    MAX_NOTE_CHARS,
    MAX_QUOTE_CHARS,
    MAX_SEGMENT_CHARS,
    MAX_SEGMENTS,
    MAX_SOURCES_PER_SEGMENT,
    MESSAGES,
    AnswerFormatError,
    AnswerSegment,
    Finding,
    FindingCode,
    SegmentStatus,
    SentSource,
    SourceError,
    SourceMap,
    StructuredAnswer,
    find_quote,
    normalise,
    parse_answer,
    validate_answer,
)

# Synthetic passages, in the style of the demo corpus.
REQUIREMENTS = (
    "Project requirements. The prototype is due October 15 and the final report\n"
    "is due December 3. Teams of four present in week 14."
)
SYLLABUS = "Late work loses five points per day. The final exam is on December 10 in Room 201."
GRADES = "Homework 30%, project 40%, final exam 30%."


def _source(chunk_id: int, text: str, page: int = 1) -> SentSource:
    return SentSource(
        chunk_id=chunk_id,
        version_id=chunk_id + 100,
        document_id=chunk_id + 1000,
        page_start=page,
        page_end=page,
        text=text,
    )


SOURCES = SourceMap([_source(11, REQUIREMENTS, 4), _source(12, SYLLABUS), _source(13, GRADES)])


def _segment(text: str, sources: list[str], quote: str | None) -> AnswerSegment:
    return AnswerSegment(text=text, sources=sources, quote=quote)


def _answer(
    *segments: AnswerSegment, flag: bool = False, missing: str | None = ""
) -> StructuredAnswer:
    return StructuredAnswer(answer=list(segments), insufficient_evidence=flag, missing=missing)


def _codes(answer: citations.ValidatedAnswer) -> list[tuple[FindingCode, int | None, int | None]]:
    return [(finding.code, finding.segment, finding.source) for finding in answer.findings]


def _one(sources: list[str], quote: str, text: str = "A claim.") -> citations.ValidatedAnswer:
    return validate_answer(_answer(_segment(text, sources, quote)), SOURCES)


# Labels


def test_labels_are_made_in_order() -> None:
    assert SOURCES.labels == ("S1", "S2", "S3")
    assert len(SOURCES) == 3
    source = SOURCES.get("S2")
    assert source is not None
    assert source.chunk_id == 12


def test_a_supported_segment_resolves_to_the_servers_ids() -> None:
    result = _one(["S1"], "prototype is due October 15")
    (segment,) = result.segments
    assert segment.status is SegmentStatus.SUPPORTED
    (citation,) = segment.citations
    assert (citation.label, citation.chunk_id, citation.version_id, citation.document_id) == (
        "S1",
        11,
        111,
        1011,
    )
    assert (citation.page_start, citation.page_end) == (4, 4)
    assert citation.verified
    assert citation.quote_span is not None
    start, end = citation.quote_span
    assert REQUIREMENTS[start:end] == "prototype is due October 15"
    assert result.findings == ()
    assert not result.insufficient_evidence
    assert result.cited_chunk_ids == frozenset({11})


@pytest.mark.parametrize(
    "label",
    [
        "S0",
        "S4",
        "S01",
        "S001",
        "s1",
        "[S1]",
        "S1\n",
        "S1\r",
        "S1 ",
        " S1",
        "S 1",
        "S-1",
        "\uff33\uff11",  # full-width letters, which NFKC would fold to S1
        "S\u0661",  # an Arabic-Indic digit one
        "1",
        "",
        "S+1",
        "S1.0",
        "chunk:11",
        "11",
    ],
)
def test_only_the_exact_label_resolves(label: str) -> None:
    assert SOURCES.get(label) is None
    result = _one([label], "prototype is due October 15")
    (segment,) = result.segments
    assert segment.status is SegmentStatus.UNSUPPORTED
    assert segment.citations == ()
    assert _codes(result) == [(FindingCode.INVENTED_LABEL, 0, 0)]
    assert result.cited_chunk_ids == frozenset()


def test_an_invented_label_is_removed_and_the_rest_kept() -> None:
    # Any invented label makes the segment unsupported (plan section 11, ADR 0011), even beside
    # a label whose passage holds the quote; that label is still resolved and checked.
    result = _one(["S9", "S1"], "prototype is due October 15")
    (segment,) = result.segments
    assert segment.status is SegmentStatus.UNSUPPORTED
    assert [citation.label for citation in segment.citations] == ["S1"]
    assert segment.citations[0].verified
    assert _codes(result) == [(FindingCode.INVENTED_LABEL, 0, 0)]
    assert result.cited_chunk_ids == frozenset({11})


@pytest.mark.parametrize(
    ("sources", "quote", "status", "codes"),
    [
        # No label at all comes first.
        ([], "prototype", SegmentStatus.UNCITED, [(FindingCode.UNCITED_SEGMENT, 0, None)]),
        # Any invented label comes next, whatever the given labels show.
        (
            ["S1", "S9"],
            "prototype",
            SegmentStatus.UNSUPPORTED,
            [(FindingCode.INVENTED_LABEL, 0, 1)],
        ),
        (
            ["S9", "S2"],
            "prototype",
            SegmentStatus.UNSUPPORTED,
            [(FindingCode.INVENTED_LABEL, 0, 0), (FindingCode.QUOTE_NOT_FOUND, 0, 1)],
        ),
        (
            ["S9", "S1"],
            "",
            SegmentStatus.UNSUPPORTED,
            [(FindingCode.INVENTED_LABEL, 0, 0), (FindingCode.MISSING_QUOTE, 0, None)],
        ),
        (
            ["S1", "S1", "s1"],
            "prototype",
            SegmentStatus.UNSUPPORTED,
            [(FindingCode.REPEATED_LABEL, 0, 1), (FindingCode.INVENTED_LABEL, 0, 2)],
        ),
        (["S9"], "", SegmentStatus.UNSUPPORTED, [(FindingCode.INVENTED_LABEL, 0, 0)]),
        # Then a quote found in a given passage.
        (["S2", "S1"], "prototype", SegmentStatus.SUPPORTED, [(FindingCode.QUOTE_NOT_FOUND, 0, 0)]),
        (["S1"], "absent", SegmentStatus.UNVERIFIED, [(FindingCode.QUOTE_NOT_FOUND, 0, 0)]),
        (["S1"], "", SegmentStatus.UNVERIFIED, [(FindingCode.MISSING_QUOTE, 0, None)]),
    ],
)
def test_status_precedence(
    sources: list[str],
    quote: str,
    status: SegmentStatus,
    codes: list[tuple[FindingCode, int | None, int | None]],
) -> None:
    result = _one(sources, quote)
    assert result.segments[0].status is status
    assert _codes(result) == codes


@pytest.mark.parametrize("cited", [False, True])
@pytest.mark.parametrize("invented", [False, True])
@pytest.mark.parametrize("verified", [False, True])
def test_status_table(cited: bool, invented: bool, verified: bool) -> None:
    expected = (
        SegmentStatus.UNCITED
        if not cited
        else SegmentStatus.UNSUPPORTED
        if invented
        else SegmentStatus.SUPPORTED
        if verified
        else SegmentStatus.UNVERIFIED
    )
    assert citations._status(cited=cited, invented=invented, verified=verified) is expected


def test_a_repeated_label_counts_once() -> None:
    result = _one(["S1", "S1", "S2", "S1"], "prototype is due October 15")
    (segment,) = result.segments
    assert [citation.label for citation in segment.citations] == ["S1", "S2"]
    assert _codes(result) == [
        (FindingCode.REPEATED_LABEL, 0, 1),
        (FindingCode.QUOTE_NOT_FOUND, 0, 2),
        (FindingCode.REPEATED_LABEL, 0, 3),
    ]


def test_a_repeated_invented_label_is_reported_once_as_invented() -> None:
    result = _one(["S7", "S7"], "prototype")
    assert _codes(result) == [
        (FindingCode.INVENTED_LABEL, 0, 0),
        (FindingCode.REPEATED_LABEL, 0, 1),
    ]


def test_get_refuses_a_label_that_is_not_a_string() -> None:
    assert SOURCES.get(1) is None  # type: ignore[arg-type]


# Segment statuses


def test_an_uncited_segment_is_kept_and_reported() -> None:
    result = _one([], "")
    (segment,) = result.segments
    assert segment.status is SegmentStatus.UNCITED
    assert _codes(result) == [(FindingCode.UNCITED_SEGMENT, 0, None)]


def test_an_uncited_segment_with_a_quote_is_still_uncited() -> None:
    result = _one([], "prototype is due October 15")
    assert result.segments[0].status is SegmentStatus.UNCITED
    assert _codes(result) == [(FindingCode.UNCITED_SEGMENT, 0, None)]


@pytest.mark.parametrize("quote", ["", " ", "\n\t", "\u00a0\u3000"])
def test_a_cited_segment_without_a_quote_is_unverified(quote: str) -> None:
    result = _one(["S1", "S2"], quote)
    (segment,) = result.segments
    assert segment.status is SegmentStatus.UNVERIFIED
    assert [citation.quote_span for citation in segment.citations] == [None, None]
    assert _codes(result) == [(FindingCode.MISSING_QUOTE, 0, None)]
    # Cited all the same: the model saw and named them.
    assert result.cited_chunk_ids == frozenset({11, 12})


def test_a_quote_found_in_no_cited_source_is_unverified() -> None:
    result = _one(["S1"], "the prototype is due November 15")
    assert result.segments[0].status is SegmentStatus.UNVERIFIED
    assert _codes(result) == [(FindingCode.QUOTE_NOT_FOUND, 0, 0)]


def test_each_citation_is_verified_on_its_own() -> None:
    result = _one(["S2", "S1"], "final report\nis due December 3")
    (segment,) = result.segments
    assert segment.status is SegmentStatus.SUPPORTED
    assert [citation.verified for citation in segment.citations] == [False, True]
    assert _codes(result) == [(FindingCode.QUOTE_NOT_FOUND, 0, 0)]


def test_a_quote_from_an_uncited_source_does_not_verify() -> None:
    # The words are in S2, but the segment cites S1.
    result = _one(["S1"], "Late work loses five points per day")
    assert result.segments[0].status is SegmentStatus.UNVERIFIED


def test_a_quote_that_spans_two_sources_is_found_in_neither() -> None:
    sources = SourceMap([_source(1, "The prototype is due"), _source(2, "October 15 at noon.")])
    answer = _answer(_segment("Due October 15.", ["S1", "S2"], "prototype is due October 15"))
    result = validate_answer(answer, sources)
    assert result.segments[0].status is SegmentStatus.UNVERIFIED
    assert _codes(result) == [
        (FindingCode.QUOTE_NOT_FOUND, 0, 0),
        (FindingCode.QUOTE_NOT_FOUND, 0, 1),
    ]


@pytest.mark.parametrize("text", ["", " ", "\n", "\u00a0"])
def test_a_blank_segment_is_removed(text: str) -> None:
    answer = _answer(
        _segment(text, ["S1"], "prototype"),
        _segment("The prototype is due October 15.", ["S1"], "prototype is due October 15"),
    )
    result = validate_answer(answer, SOURCES)
    assert len(result.segments) == 1
    assert _codes(result) == [(FindingCode.EMPTY_SEGMENT, 0, None)]


def test_findings_name_the_position_in_the_models_list() -> None:
    answer = _answer(
        _segment("First.", ["S1"], "prototype"),
        _segment("", [], ""),
        _segment("Third.", ["S2", "S8"], "December 10"),
    )
    result = validate_answer(answer, SOURCES)
    assert _codes(result) == [
        (FindingCode.EMPTY_SEGMENT, 1, None),
        (FindingCode.INVENTED_LABEL, 2, 1),
    ]
    assert [segment.status for segment in result.segments] == [
        SegmentStatus.SUPPORTED,
        SegmentStatus.UNSUPPORTED,
    ]


# The flag


def test_no_segment_without_the_flag_is_an_empty_answer() -> None:
    result = validate_answer(_answer(), SOURCES)
    assert _codes(result) == [(FindingCode.EMPTY_ANSWER, None, None)]
    assert result.insufficient_evidence


def test_only_blank_segments_is_an_empty_answer() -> None:
    result = validate_answer(_answer(_segment(" ", [], "")), SOURCES)
    assert _codes(result) == [
        (FindingCode.EMPTY_SEGMENT, 0, None),
        (FindingCode.EMPTY_ANSWER, None, None),
    ]
    assert result.insufficient_evidence


def test_the_flag_with_a_note_and_no_claims_is_clean() -> None:
    result = validate_answer(_answer(flag=True, missing="No document gives the date."), SOURCES)
    assert result.findings == ()
    assert result.insufficient_evidence
    assert result.missing == "No document gives the date."


def test_the_flag_with_claims_is_reported_and_kept() -> None:
    answer = _answer(
        _segment("The prototype is due October 15.", ["S1"], "prototype is due October 15"),
        flag=True,
        missing="The time of day is not given.",
    )
    result = validate_answer(answer, SOURCES)
    assert _codes(result) == [(FindingCode.FLAG_WITH_CLAIMS, None, None)]
    assert result.insufficient_evidence
    assert len(result.segments) == 1


@pytest.mark.parametrize("missing", ["", " ", "\n"])
def test_the_flag_without_a_note_is_reported(missing: str) -> None:
    result = validate_answer(_answer(flag=True, missing=missing), SOURCES)
    assert _codes(result) == [(FindingCode.MISSING_NOTE, None, None)]


def test_a_note_without_the_flag_is_allowed() -> None:
    answer = _answer(
        _segment("The exam is on December 10.", ["S2"], "final exam is on December 10"),
        missing="The time of the exam is not given.",
    )
    result = validate_answer(answer, SOURCES)
    assert result.findings == ()
    assert not result.insufficient_evidence


def test_an_empty_source_map_supports_nothing() -> None:
    empty = SourceMap([])
    assert empty.labels == ()
    result = validate_answer(_answer(_segment("A claim.", ["S1"], "prototype")), empty)
    assert result.segments[0].status is SegmentStatus.UNSUPPORTED


# Quote matching


@pytest.mark.parametrize(
    ("quote", "text"),
    [
        ("prototype is due October 15", REQUIREMENTS),
        ("final report is due December 3", REQUIREMENTS),  # a line break in the passage
        ("final   report\n\n is due", REQUIREMENTS),  # runs of white space in the quote
        ("  prototype is due  ", REQUIREMENTS),  # the quote's ends are trimmed
        ("due\u00a0October\u202f15", REQUIREMENTS),  # no-break spaces
        ("week\t14", REQUIREMENTS),
        # CR LF is one grapheme cluster, made only of white space, so it is a run too.
        ("final report is due", "final report\r\nis due"),  # CR LF in the passage
        ("final report\r\nis due", REQUIREMENTS),  # CR LF in the quote
        ("final report\r\nis due", "final report\r\nis due"),
        ("a b c d", "a\r\n\r\nb\r\n \tc\n\r\r\nd"),  # runs that mix CR LF, CR and LF
        ("a\r\n\u00a0\r b", "a b"),
        ("a b", "a\u2028\r\n\u3000b"),
        ("the team's report", "Read the team\u2019s report."),
        ("the team\u2019s report", "Read the team's report."),
        ('"final"', "The \u201cfinal\u201d draft."),
        ("\u201efinal\u201f", 'The "final" draft.'),
        ("pages 4-5", "See pages 4\u20135."),
        ("pages 4\u20145", "See pages 4-5."),
        ("x \u2212 y", "where x - y holds"),
        ("caf\u00e9", "the cafe\u0301 menu"),  # NFC: composed against decomposed
        ("cafe\u0301", "the caf\u00e9 menu"),
        ("\u212b", "1 \u00c5 apart"),  # the angstrom sign is a singleton NFC maps
        ("\uac01", "\u1100\u1161\u11a8 x"),  # Hangul jamo compose under NFC
        ("Room 201.", SYLLABUS),  # at the very end
        ("Late", SYLLABUS),  # at the very start
    ],
)
def test_quotes_found_after_the_documented_folding(quote: str, text: str) -> None:
    span = find_quote(quote, text)
    assert span is not None
    start, end = span
    assert normalise(text[start:end]) == normalise(quote)


@pytest.mark.parametrize(
    ("quote", "text"),
    [
        ("PROTOTYPE IS DUE", REQUIREMENTS),  # case
        ("prototype is due oct 15", REQUIREMENTS),  # an abbreviation
        ("prototype is due October 15", "The prototype is due October\u00ad 15"),  # soft hyphen
        ("prototype is due", "The proto\u200btype is due"),  # zero-width space
        ("the \ufb01le", "the file"),  # a ligature: NFKC would fold it
        ("the file", "the \ufb01le"),
        ("\uff11\uff15", "October 15"),  # full-width digits
        ("x2", "x\u00b2"),  # a superscript
        ("and so...", "and so\u2026"),  # an ellipsis
        ("5'", "5\u2032"),  # a prime is not a quotation mark
        ("\u00abfinal\u00bb", '"final"'),  # guillemets are not folded
        ("prototype is due, October 15", REQUIREMENTS),  # punctuation
        ("prototypeisdue", REQUIREMENTS),  # white space is not optional
        ("prototype\x1fis due", REQUIREMENTS),  # an information separator is not white space
        ("cafe", "the caf\u00e9 menu"),  # accents are not removed
        ("q", "q\u0303"),  # a base letter without its mark
        ("\u0303", "q\u0303"),  # a mark without its base letter
        ("\u1100\u1161", "\uac01"),  # half of a Hangul syllable
        ("Room 201. Extra", SYLLABUS),  # past the end
        # Part of a grapheme cluster (UAX #29) is not a whole character.
        ("\u0915", "\u0915\u093e"),  # a consonant without its vowel sign (Mc, class 0)
        ("\u0915", "\u0915\u0903"),  # without its visarga
        ("\u0e01", "\u0e01\u0e33"),  # a Thai consonant without sara am
        ("\u2764", "\u2764\ufe0f"),  # an emoji without its variation selector
        ("\u845b", "\u845b\U000e0100"),  # an ideograph without its variation selector
        ("\U0001f44d", "\U0001f44d\U0001f3fd"),  # without its skin tone modifier
        ("\U0001f468", "\U0001f468\u200d\U0001f469\u200d\U0001f467"),  # part of a ZWJ sequence
        ("\U0001f469", "\U0001f468\u200d\U0001f469\u200d\U0001f467"),
        ("\U0001f1fa", "\U0001f1fa\U0001f1f8"),  # half a flag
        ("\U0001f1f8\U0001f1ea", "\U0001f1fa\U0001f1f8\U0001f1ea\U0001f1f8"),  # across two flags
        ("\u0661", "\u0600\u0661"),  # a number without its prepended sign
        ("\u1100", "\u1100\u1161"),  # a Hangul initial without its vowel
        # A space carrying a mark is not a run of white space, and a run is not such a space.
        (" \ufe0f", "a \n\ufe0f"),
        ("a \n\ufe0f", "a \ufe0f"),
        ("a b", "a\r\n\u0301b"),  # a mark after CR LF stands alone and is not white space
        ("a \u0301 b", "a \r\n b"),
        ("a b", "a \u0301b"),  # a space carrying a mark is not a run
    ],
)
def test_quotes_not_found_without_over_broad_folding(quote: str, text: str) -> None:
    assert find_quote(quote, text) is None


def test_the_span_is_in_the_text_as_sent() -> None:
    text = "Intro.\r\n\r\nThe  caf\u00e9\u2019s   \u201cmenu\u201d opens at 9."
    span = find_quote('caf\u00e9\'s "menu" opens', text)
    assert span is not None
    assert text[span[0] : span[1]] == "caf\u00e9\u2019s   \u201cmenu\u201d opens"


@pytest.mark.parametrize(
    "text",
    [
        "\u0915\u093e",
        "\u2764\ufe0f",
        "\U0001f44d\U0001f3fd",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",
        "\U0001f1fa\U0001f1f8",
        "\u0600\u0661",
    ],
)
def test_a_whole_cluster_is_found_with_its_span(text: str) -> None:
    passage = f"Before {text} after."
    assert find_quote(f"Before {text} after", passage) == (0, len(passage) - 1)
    assert find_quote(text, passage) == (7, 7 + len(text))


def test_the_span_covers_combining_marks_and_composed_hangul() -> None:
    text = "a cafe\u0301 \u1100\u1161\u11a8 b"
    span = find_quote("caf\u00e9 \uac01", text)
    assert span is not None
    assert text[span[0] : span[1]] == "cafe\u0301 \u1100\u1161\u11a8"


def test_a_misaligned_first_match_does_not_hide_a_later_one() -> None:
    text = "q\u0303 then q alone"
    span = find_quote("q", text)
    assert span == (8, 9)


def test_a_quote_as_long_as_the_limit_is_matched() -> None:
    text = ("word " * 400).strip()
    quote = text[:MAX_QUOTE_CHARS]
    sources = SourceMap([_source(1, text)])
    answer = _answer(_segment("A long claim.", ["S1"], quote.rstrip()))
    result = validate_answer(answer, sources)
    assert result.segments[0].status is SegmentStatus.SUPPORTED


def test_a_quote_longer_than_the_passage_is_not_found() -> None:
    assert find_quote(GRADES + " More.", GRADES) is None


def test_normalise_changes_only_what_it_documents() -> None:
    assert normalise(" a\u2018b\u2019c\u201cd\u201de\u2013f\u2014g\u2212h \u00a0\n i ") == (
        "a'b'c\"d\"e-f-g-h i"
    )
    assert normalise("A\u2026\u00ad\u200b\ufb01\u2032") == "A\u2026\u00ad\u200b\ufb01\u2032"


def test_whitespace_set_is_spaces_and_breaks_only() -> None:
    zs = {chr(cp) for cp in range(0x110000) if unicodedata.category(chr(cp)) == "Zs"}
    assert set(citations.WHITESPACE) == zs | set("\t\n\v\f\r\x85\u2028\u2029")
    assert not set("\x1c\x1d\x1e\x1f\u200b\u00ad") & citations.WHITESPACE


def _decomposition_alphabet() -> list[str]:
    """Characters that take part in canonical composition, with a few starters and marks."""
    chars = set(
        "\n\u200d\ufe0f\u0915\u093eae Aq\u0301\u0308\u0323\u0327\u0345"
        "\u1100\u1161\u11a8\uac00\u212b\u0958"
    )
    for cp in range(0x30000):
        parts = unicodedata.decomposition(chr(cp)).split()
        if len(parts) == 2 and not parts[0].startswith("<"):
            chars.update({chr(cp), chr(int(parts[0], 16)), chr(int(parts[1], 16))})
    return sorted(chars)


def test_characters_normalise_independently() -> None:
    # The oracle is NFC of the whole string, not the rule the code uses to cut it.
    alphabet = _decomposition_alphabet()
    rng = random.Random(485)  # noqa: S311 - a fixed seed for a repeatable test
    for _ in range(30_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
        pieces = [unicodedata.normalize("NFC", text[a:b]) for a, b in citations._characters(text)]
        assert "".join(pieces) == unicodedata.normalize("NFC", text)


def _reference_find(quote: str, text: str) -> bool:
    """Brute force: some slice of ``text`` between two character boundaries normalises to the
    quote. A boundary is a grapheme cluster boundary (checked on its own against Perl's ``\\X``
    in ``test_graphemes``) where NFC of the parts equals NFC of the whole."""
    needle = normalise(quote)
    if not needle:
        return False
    whole = unicodedata.normalize("NFC", text)
    clusters = graphemes.boundaries(text)
    cuts = [
        i
        for i in range(len(text) + 1)
        if unicodedata.normalize("NFC", text[:i]) + unicodedata.normalize("NFC", text[i:]) == whole
        and clusters[i]
    ]
    return any(normalise(text[i:j]) == needle for i in cuts for j in cuts if i < j)


def test_find_quote_agrees_with_a_brute_force_reference() -> None:
    alphabet = [
        *"ab ",
        "\n",
        "\r",
        "\r\n",
        "\t",
        "\u00a0",
        "\u2019",
        "'",
        "\u2013",
        "-",
        "\u0301",
        "\u0303",
        "\u00e9",
        "e",
        "\u1100",
        "\u1161",
        "\u11a8",
        "\uac00",
        "\u0915",
        "\u093e",
        "\u2764",
        "\ufe0f",
        "\u200d",
        "\U0001f1fa",
        "\U0001f1f8",
    ]
    rng = random.Random(15)  # noqa: S311 - a fixed seed for a repeatable test
    found = 0
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 7)))
        if rng.random() < 0.5:
            start = rng.randint(0, len(text))
            quote = text[start : rng.randint(start, len(text))]
        else:
            quote = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 3)))
        span = find_quote(quote, text)
        assert (span is not None) == _reference_find(quote, text), (quote, text)
        if span is not None:
            found += 1
            assert normalise(text[span[0] : span[1]]) == normalise(quote)
    assert found > 1_000


# Sources


@pytest.mark.parametrize(
    "fields",
    [
        {"chunk_id": 0},
        {"chunk_id": -1},
        {"chunk_id": True},
        {"version_id": 1.0},
        {"document_id": "7"},
        {"page_start": 0},
        {"page_start": 3, "page_end": 2},
        {"text": ""},
        {"text": " \n"},
        {"text": b"bytes"},
    ],
)
def test_a_malformed_source_is_refused(fields: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "chunk_id": 1,
        "version_id": 2,
        "document_id": 3,
        "page_start": 1,
        "page_end": 1,
        "text": "Some text.",
    }
    values.update(fields)
    with pytest.raises(SourceError):
        SentSource(**values)


def test_a_chunk_given_twice_is_refused() -> None:
    with pytest.raises(SourceError, match="twice"):
        SourceMap([_source(1, "one"), _source(1, "one again")])


def test_a_source_map_takes_only_sources() -> None:
    with pytest.raises(SourceError):
        SourceMap([{"chunk_id": 1}])  # type: ignore[list-item]


def test_source_text_is_left_out_of_its_repr() -> None:
    assert "prototype" not in repr(SOURCES.get("S1"))


# The schema


def _json(**overrides: object) -> str:
    data: dict[str, object] = {
        "answer": [{"text": "The prototype is due October 15.", "sources": ["S1"], "quote": "x"}],
        "insufficient_evidence": False,
        "missing": "",
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_answer_reads_the_plans_shape() -> None:
    answer = parse_answer(_json())
    assert answer.insufficient_evidence is False
    assert answer.answer[0].sources == ["S1"]


def test_parse_answer_reads_null_as_no_quote_and_no_note() -> None:
    answer = parse_answer(
        _json(answer=[{"text": "A claim.", "sources": ["S1"], "quote": None}], missing=None)
    )
    assert answer.answer[0].quote is None
    assert answer.missing is None


def test_a_null_quote_is_a_missing_quote() -> None:
    result = validate_answer(
        _answer(AnswerSegment(text="A claim.", sources=["S1"], quote=None)), SOURCES
    )
    (segment,) = result.segments
    assert segment.status is SegmentStatus.UNVERIFIED
    assert segment.quote is None
    assert segment.citations[0].quote_span is None
    assert _codes(result) == [(FindingCode.MISSING_QUOTE, 0, None)]


def test_a_null_quote_on_an_uncited_segment_is_uncited() -> None:
    result = validate_answer(
        _answer(AnswerSegment(text="A claim.", sources=[], quote=None)), SOURCES
    )
    assert result.segments[0].status is SegmentStatus.UNCITED
    assert _codes(result) == [(FindingCode.UNCITED_SEGMENT, 0, None)]


def test_the_flag_with_a_null_note_is_reported() -> None:
    result = validate_answer(_answer(flag=True, missing=None), SOURCES)
    assert result.missing is None
    assert result.insufficient_evidence is True
    assert _codes(result) == [(FindingCode.MISSING_NOTE, None, None)]


def test_a_null_note_without_the_flag_is_clean() -> None:
    answer = _answer(_segment("The prototype is due.", ["S1"], "due October 15"), missing=None)
    result = validate_answer(answer, SOURCES)
    assert result.findings == ()
    assert result.segments[0].status is SegmentStatus.SUPPORTED


@pytest.mark.parametrize(
    "raw",
    [
        _json(extra="field"),
        _json(answer=[{"text": "t", "sources": [], "quote": "", "page": 4}]),
        _json(answer=[{"text": "t", "sources": []}]),
        _json(answer=[{"text": "t", "sources": "S1", "quote": ""}]),
        _json(answer=[{"text": "t", "sources": [1], "quote": ""}]),
        _json(answer=[{"text": 5, "sources": [], "quote": ""}]),
        _json(answer={"text": "t"}),
        _json(insufficient_evidence="false"),
        _json(insufficient_evidence=0),
        _json(insufficient_evidence=None),
        json.dumps({"answer": [], "insufficient_evidence": False}),
        json.dumps([]),
        "null",
        "",
        "{",
        "not json",
        '{"answer": [], "insufficient_evidence": false, "missing": "", "missing": "x"}',
        '{"answer": [], "insufficient_evidence": false, "missing": "\\ud800"}',
        "[" * 100_000 + "]" * 100_000,
        _json(answer=[{"text": "t", "sources": [], "quote": ""}] * (MAX_SEGMENTS + 1)),
        _json(answer=[{"text": "t" * (MAX_SEGMENT_CHARS + 1), "sources": [], "quote": ""}]),
        _json(
            answer=[{"text": "t", "sources": ["S1"] * (MAX_SOURCES_PER_SEGMENT + 1), "quote": ""}]
        ),
        _json(answer=[{"text": "t", "sources": ["S" * (MAX_LABEL_CHARS + 1)], "quote": ""}]),
        _json(answer=[{"text": "t", "sources": [], "quote": "q" * (MAX_QUOTE_CHARS + 1)}]),
        _json(missing="m" * (MAX_NOTE_CHARS + 1)),
        " " * (MAX_ANSWER_CHARS + 1),
    ],
)
def test_parse_answer_refuses_anything_else_with_fixed_text(raw: str) -> None:
    with pytest.raises(AnswerFormatError) as caught:
        parse_answer(raw)
    assert str(caught.value) in {
        "the answer is not JSON text within the size limit",
        "the answer is not valid JSON",
        "the answer does not match the answer schema",
    }
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_parse_answer_refuses_what_is_not_text() -> None:
    with pytest.raises(AnswerFormatError):
        parse_answer(b"{}")  # type: ignore[arg-type]


def test_parse_answer_accepts_the_limits() -> None:
    raw = _json(
        answer=[
            {
                "text": "t" * MAX_SEGMENT_CHARS,
                "sources": ["S" * MAX_LABEL_CHARS] * MAX_SOURCES_PER_SEGMENT,
                "quote": "q" * MAX_QUOTE_CHARS,
            }
        ],
        missing="m" * MAX_NOTE_CHARS,
    )
    assert len(parse_answer(raw).answer[0].sources) == MAX_SOURCES_PER_SEGMENT


def test_parse_errors_do_not_repeat_the_input() -> None:
    private = "Patient-Name diagnosis"
    for raw in (_json(**{private: 1}), _json(missing=[private]), private):
        with pytest.raises(AnswerFormatError) as caught:
            parse_answer(raw)
        assert private not in str(caught.value)
        assert "Patient" not in repr(caught.value)


def test_the_schema_refuses_coercion_in_code_too() -> None:
    with pytest.raises(ValueError, match="validation error"):
        StructuredAnswer.model_validate(
            {"answer": [], "insufficient_evidence": 1, "missing": ""}, strict=True
        )


def test_the_json_schema_forbids_unknown_fields() -> None:
    schema = StructuredAnswer.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["answer", "insufficient_evidence", "missing"]
    segment = schema["$defs"]["AnswerSegment"]
    assert segment["additionalProperties"] is False
    assert segment["required"] == ["text", "sources", "quote"]


def test_model_reprs_leave_out_text() -> None:
    answer = parse_answer(_json(missing="secret note"))
    assert "prototype" not in repr(answer)
    assert "secret note" not in repr(answer)
    result = validate_answer(answer, SOURCES)
    assert "prototype" not in repr(result)
    assert "secret note" not in repr(result)


# Findings


def test_every_code_has_fixed_text() -> None:
    assert set(MESSAGES) == set(FindingCode)
    assert Finding(FindingCode.UNCITED_SEGMENT, 0).message == "The segment cites no source."


def test_every_code_can_be_produced() -> None:
    answers = [
        _answer(_segment("a", ["S9"], "x")),
        _answer(_segment("a", ["S1", "S1"], "prototype")),
        _answer(_segment("a", [], "")),
        _answer(_segment("a", ["S1"], "")),
        _answer(_segment("a", ["S1"], "absent words")),
        _answer(_segment("", [], ""), _segment("b", [], "")),
        _answer(),
        _answer(_segment("a", [], ""), flag=True, missing="m"),
        _answer(flag=True),
    ]
    produced = {
        finding.code for answer in answers for finding in validate_answer(answer, SOURCES).findings
    }
    assert produced == set(FindingCode)
