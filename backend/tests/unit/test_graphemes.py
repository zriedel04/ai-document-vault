"""Extended grapheme clusters (UAX #29, Unicode 15.0.0): the tables and the rules."""

from __future__ import annotations

import itertools
import unicodedata
from pathlib import Path

import pytest

from vault.answers import graphemes
from vault.answers.graphemes import Break, boundaries, is_pictographic, property_of

CASES = Path(__file__).parents[1] / "fixtures" / "unicode" / "grapheme_break_cases.txt"


def _cases() -> list[tuple[str, list[bool]]]:
    cases = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        text = "".join(chr(int(token, 16)) for token in tokens[1::2])
        marks = [mark == "\u00f7" for mark in tokens[0::2]]
        assert set(tokens[0::2]) <= {"\u00f7", "\u00d7"}
        cases.append((text, marks))
    return cases


def test_python_has_the_tables_unicode_version() -> None:
    # The tables were written for this version; another would disagree with unicodedata.
    assert unicodedata.unidata_version == graphemes.UNICODE_VERSION


def test_boundaries_agree_with_perls_grapheme_clusters() -> None:
    cases = _cases()
    assert len(cases) > 5_000
    for text, marks in cases:
        assert boundaries(text) == marks, [f"{ord(c):04X}" for c in text]


@pytest.mark.parametrize(
    ("text", "clusters"),
    [
        ("", []),
        ("ab", ["a", "b"]),
        ("\r\n", ["\r\n"]),
        (
            "\n\u0308",
            ["\n", "\u0308"],
        ),  # GB4: a control ends a cluster, a mark after it stands alone
        ("\u0915\u093e", ["\u0915\u093e"]),  # a spacing vowel sign (Mc, ccc 0)
        ("\u0915\u0903", ["\u0915\u0903"]),  # a visarga
        ("\u0e01\u0e33", ["\u0e01\u0e33"]),  # Thai sara am, a SpacingMark letter
        ("\u2764\ufe0f", ["\u2764\ufe0f"]),  # a variation selector
        ("\U0001f44d\U0001f3fd", ["\U0001f44d\U0001f3fd"]),  # an emoji modifier
        (
            "\U0001f468\u200d\U0001f469\u200d\U0001f467",
            ["\U0001f468\u200d\U0001f469\u200d\U0001f467"],
        ),
        ("a\u200d\U0001f469", ["a\u200d", "\U0001f469"]),  # GB11 needs a pictograph before ZWJ
        (
            "\U0001f1fa\U0001f1f8\U0001f1ea\U0001f1f8",
            ["\U0001f1fa\U0001f1f8", "\U0001f1ea\U0001f1f8"],
        ),
        ("\U0001f1fa\U0001f1f8\U0001f1ea", ["\U0001f1fa\U0001f1f8", "\U0001f1ea"]),
        ("\u0600\u0661", ["\u0600\u0661"]),  # a prepended concatenation mark
        ("\u1100\u1161\u11a8", ["\u1100\u1161\u11a8"]),  # Hangul jamo L V T
        ("\uac00\u11a8", ["\uac00\u11a8"]),  # LV T
        ("\uac01\u1161", ["\uac01", "\u1161"]),  # LVT then V
    ],
)
def test_rule_examples(text: str, clusters: list[str]) -> None:
    marks = boundaries(text)
    cuts = [index for index, mark in enumerate(marks) if mark]
    assert [text[a:b] for a, b in itertools.pairwise(cuts)] == clusters


def _table_ranges() -> list[graphemes.Ranges]:
    return [
        graphemes.CONTROL,
        graphemes.EXTEND,
        graphemes.PREPEND,
        graphemes.SPACING_MARK,
        graphemes.EXTENDED_PICTOGRAPHIC,
    ]


def test_tables_are_sorted_and_disjoint() -> None:
    for ranges in _table_ranges():
        for (start, end), (next_start, _) in itertools.pairwise(ranges):
            assert start <= end < next_start
    values = [(start, end) for ranges in _table_ranges()[:-1] for start, end in ranges]
    values.sort()
    for (_, end), (next_start, _) in itertools.pairwise(values):
        assert end < next_start


# UAX #29 table 2: the spacing combining marks that are not SpacingMark (Myanmar, Tai Tham, Tai
# Viet and Ahom letters and signs that take part in syllables differently).
SPACING_MARK_EXCEPTIONS = frozenset(
    {
        0x102B,
        0x102C,
        0x1038,
        *range(0x1062, 0x1065),
        *range(0x1067, 0x106E),
        0x1083,
        *range(0x1087, 0x108D),
        0x108F,
        *range(0x109A, 0x109D),
        0x1A61,
        0x1A63,
        0x1A64,
        0xAA7B,
        0xAA7D,
        0x11720,
        0x11721,
    }
)
_MARKS = frozenset({Break.SPACING_MARK, Break.EXTEND})


def _all() -> list[str]:
    return [chr(cp) for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]


def test_the_property_agrees_with_unicodedata() -> None:
    # What the character database says about each value, checked for every code point.
    hangul = {"L": "CHOSEONG", "V": "JUNGSEONG", "T": "JONGSEONG"}
    for char in _all():
        value = property_of(char)
        category = unicodedata.category(char)
        name = unicodedata.name(char, "")
        if category in {"Mn", "Me"} or 0x1F3FB <= ord(char) <= 0x1F3FF:
            assert value is Break.EXTEND, hex(ord(char))
        if category in {"Cc", "Zl", "Zp"} and char not in "\r\n":
            assert value is Break.CONTROL, hex(ord(char))
        if category == "Mc":
            expected = {Break.OTHER} if ord(char) in SPACING_MARK_EXCEPTIONS else _MARKS
            assert value in expected, hex(ord(char))
        if value is Break.EXTEND:
            assert category in {"Mn", "Me", "Mc", "Cf", "Sk", "Lm"}, hex(ord(char))
        elif value is Break.SPACING_MARK:
            assert category == "Mc" or char in "\u0e33\u0eb3", hex(ord(char))
        elif value is Break.CONTROL:
            assert category in {"Cc", "Cf", "Zl", "Zp", "Cn"}, hex(ord(char))
        elif value is Break.PREPEND:
            assert category in {"Cf", "Lo", "Mc"}, hex(ord(char))
        elif value in {Break.L, Break.V, Break.T}:
            assert hangul[value.name] in name, hex(ord(char))
        elif value in {Break.LV, Break.LVT}:
            assert name.startswith("HANGUL SYLLABLE"), hex(ord(char))
            parts = len(unicodedata.normalize("NFD", char))
            assert parts == (2 if value is Break.LV else 3)
        if name.startswith(("HANGUL CHOSEONG", "HANGUL JUNGSEONG", "HANGUL JONGSEONG")) and (
            0x1100 <= ord(char) <= 0x11FF or 0xA960 <= ord(char) <= 0xD7FF
        ):
            assert value in {Break.L, Break.V, Break.T}, hex(ord(char))
    assert property_of("\r") is Break.CR
    assert property_of("\n") is Break.LF
    assert property_of("\u200d") is Break.ZWJ
    assert property_of("\U0001f1e6") is Break.REGIONAL_INDICATOR
    assert property_of("a") is Break.OTHER


def test_pictographs() -> None:
    for char in "\u00a9\u00ae\u203c\u2764\U0001f476\U0001f468\U0001fffd":
        assert is_pictographic(char), hex(ord(char))
    for char in "a1\u0000\U0001f1e6\U0001f3fb#*":
        assert not is_pictographic(char), hex(ord(char))
