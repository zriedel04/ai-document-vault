"""Server-side citation validation (plan section 11, ADR 0011), pure and provider-neutral.

The passages sent to the model are labelled ``S1`` to ``Sn`` by :class:`SourceMap`, in the order
given, and the model answers with a :class:`StructuredAnswer`: segments, each with the labels that
support it and one short quote, an ``insufficient_evidence`` flag and a note on what is missing.
:func:`validate_answer` resolves the labels to the database ids the server holds and checks every
quote against the text that was sent; the model never supplies an id.

**The shape.** :class:`StructuredAnswer` is the one definition of the answer: the schema sent to
the provider (``vault.ai.answer.ANSWER_SCHEMA``) is its JSON schema (:func:`answer_json_schema`),
and both readers, :func:`parse_answer` for the model's JSON text and ``vault.ai.answer.read_answer``
for a provider's parsed output, go through :func:`read_structured`. Every field is required and no
other field is allowed (what strict structured output on both providers expects); ``quote`` and
``missing`` may be ``null`` for "no quote" and "nothing missing", which the rules below treat as
blank. The size bounds (``MAX_*`` but ``MAX_ANSWER_CHARS``) are in the schema too. A label is any
short text with no pattern, in the schema and here alike: a label the server did not give is
``invented_label`` (ADR 0011: removed and the segment unsupported), not a refused answer.

Rules (each failure is a :class:`Finding` with a fixed code and fixed text):

- **Labels.** A label is accepted only exactly as :class:`SourceMap` writes it (``S3``): ``[S3]``,
  ``s3``, ``S03``, ``S0``, ``S3`` with a space or a line break, and a label past ``Sn`` are not
  labels that were given, and each is removed as ``invented_label``. A label given twice in one
  segment counts once (``repeated_label``).
- **Segment status**, by precedence. ``uncited``: the segment names no label (shown muted).
  ``unsupported``: it names at least one label that was not given, whatever its other labels
  show; the labels that were given are still resolved and checked. ``supported``: the quote is
  found in at least one cited passage. ``unverified``: anything less (a blank or ``null`` quote,
  ``missing_quote``, or one found in no cited passage). Each citation records its own match, so a
  quote found in ``S3`` and not in ``S5`` verifies ``S3`` only (``quote_not_found`` for ``S5``).
- **Quote matching.** The quote must occur in one cited passage's text, as a whole: a quote that
  runs across two passages is found in neither. Both texts are compared after
  :func:`normalise` and nothing else, which folds only what a model changes when it copies text:
  Unicode NFC, the curly and low single and double quotation marks to ``'`` and ``"``, the
  hyphens, dashes and minus sign of ``DASHES`` to ``-``, and every run of white space
  (``WHITESPACE``: space separators, tab, line and paragraph breaks, CR LF included; a white space
  character that carries a combining mark is compared as it is) to one space, with the quote's
  ends trimmed. Case, other punctuation, ellipses, primes, compatibility forms (NFKC: ligatures,
  full-width letters, superscripts), zero-width characters and soft hyphens are compared as they
  are, so a quote that only matches once those are folded is not found. A match starts and ends
  on whole characters of the passage, its extended grapheme clusters (UAX #29): ``q`` is not found
  in ``q``
  with a combining tilde, a consonant without its vowel sign, an emoji without its variation
  selector, modifier or the rest of its ZWJ sequence, or one regional indicator of a flag. Its
  span is given in code points of the passage text as it was sent, for the highlight.
- **The flag.** With ``insufficient_evidence`` set the answer should hold no segments
  (``flag_with_claims``) and should say what is missing (``missing_note``, for a blank or ``null``
  note). Without it, an answer with no segment left is ``empty_answer``. Segments whose text is
  blank are removed (``empty_segment``). :attr:`ValidatedAnswer.insufficient_evidence` is true when
  the flag is set or no segment is left; weak retrieval scores are judged by the caller.

The model's output is untrusted text, as document text is: nothing here logs it, and no message or
finding repeats a label, a quote, an answer or a passage. A finding names a segment and a label by
their positions in the model's lists.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from types import MappingProxyType
from typing import Annotated, Any, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from vault.answers import graphemes
from vault.db.ids import is_ulid

# Bounds on what one answer may hold, so a malformed or hostile response costs bounded work. They
# are far above a real answer (the plan sends 8-12 passages and asks for short quotes).
MAX_ANSWER_CHARS: Final = 200_000
MAX_SEGMENTS: Final = 100
MAX_SEGMENT_CHARS: Final = 4_000
MAX_SOURCES_PER_SEGMENT: Final = 16
MAX_LABEL_CHARS: Final = 16
MAX_QUOTE_CHARS: Final = 1_000
MAX_NOTE_CHARS: Final = 2_000

LABEL_PREFIX: Final = "S"

SINGLE_QUOTES: Final = frozenset("\u2018\u2019\u201a\u201b")
DOUBLE_QUOTES: Final = frozenset("\u201c\u201d\u201e\u201f")
# Hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar, minus sign.
DASHES: Final = frozenset("\u2010\u2011\u2012\u2013\u2014\u2015\u2212")
# Every space separator (category Zs: space, no-break space, the typographic spaces, ideographic
# space), tab, the line breaks and the line and paragraph separators. Not ``str.isspace``, which
# also counts the information separators U+001C to U+001F.
WHITESPACE: Final = frozenset(
    "\t\n\v\f\r\x85\u2028\u2029"
    + "".join(chr(cp) for cp in range(0x10000) if unicodedata.category(chr(cp)) == "Zs")
)

# Hangul vowel and trailing consonant jamo compose with the syllable before them by algorithm,
# not by a decomposition in the character data.
_HANGUL_V: Final = range(0x1161, 0x1176)
_HANGUL_T: Final = range(0x11A8, 0x11C3)
# Every canonical decomposition lies below this code point.
_DECOMPOSITION_END: Final = 0x30000


class AnswerFormatError(ValueError):
    """The model's answer is not a :class:`StructuredAnswer`. The message is fixed text."""


class SourceError(ValueError):
    """A source given to :class:`SourceMap` is malformed (a caller's mistake). Fixed text."""


def _utf8(value: str) -> str:
    # A JSON escape can spell a lone surrogate, which no UTF-8 store or page can hold.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        msg = "text holds a lone surrogate"
        raise ValueError(msg) from None
    return value


Text = Annotated[str, AfterValidator(_utf8)]


def _without_description(schema: dict[str, Any]) -> None:
    schema.pop("description", None)


class _Strict(BaseModel):
    # Unknown fields and values of the wrong kind (a number for a string, ``"true"`` or ``1`` for
    # the flag, an object for a list) are refused, not coerced. hide_input_in_errors: a
    # ValidationError never repeats the model's text, should one reach a caller.
    # The JSON schema is sent to the model, so a class docstring (written for this code) is left
    # out of it: what the model is told belongs to the prompt.
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
        json_schema_extra=_without_description,
    )


Label = Annotated[Text, Field(max_length=MAX_LABEL_CHARS)]
Quote = Annotated[Text, Field(max_length=MAX_QUOTE_CHARS)]
Note = Annotated[Text, Field(max_length=MAX_NOTE_CHARS)]


class AnswerSegment(_Strict):
    """One sentence or short segment of the answer, as the model wrote it."""

    text: Annotated[Text, Field(max_length=MAX_SEGMENT_CHARS, repr=False)]
    sources: Annotated[list[Label], Field(max_length=MAX_SOURCES_PER_SEGMENT, repr=False)]
    # ``null``: no quote, which the validator treats as a blank one (``missing_quote``).
    quote: Annotated[Quote | None, Field(repr=False)]


class StructuredAnswer(_Strict):
    """The model's structured output: every field is required, and no other field is allowed.

    The one definition of the answer's shape: the provider schema and both readers come from it,
    so they cannot drift apart."""

    answer: Annotated[list[AnswerSegment], Field(max_length=MAX_SEGMENTS)]
    insufficient_evidence: bool
    # ``null``: nothing is said to be missing, which the validator treats as a blank note.
    missing: Annotated[Note | None, Field(repr=False)]


def answer_json_schema() -> dict[str, object]:
    """The JSON schema of :class:`StructuredAnswer`, as sent to a provider (a fresh copy)."""
    return StructuredAnswer.model_json_schema()


def read_structured(data: object) -> StructuredAnswer:
    """``data``, a value a JSON reader built, as a :class:`StructuredAnswer`, or
    :class:`AnswerFormatError` with fixed text and no cause (the schema's messages quote it)."""
    try:
        return StructuredAnswer.model_validate(data, strict=True)
    except ValidationError:
        msg = "the answer does not match the answer schema"
        raise AnswerFormatError(msg) from None


def _no_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        msg = "repeated key"
        raise ValueError(msg)
    return dict(pairs)


def parse_answer(raw: str) -> StructuredAnswer:
    """The model's JSON text as a :class:`StructuredAnswer`, or :class:`AnswerFormatError`.

    The text is bounded first (``MAX_ANSWER_CHARS``), then read as JSON with an object that names
    a key twice refused (a JSON reader would keep one of the two silently), then checked against
    the schema in strict mode (:func:`read_structured`). The size bound is on the text before it
    is parsed, a cost bound and not part of the shape: a provider's output is bounded by its
    ``max_output_tokens`` instead. The error is fixed text and is raised without its cause,
    because the JSON reader's and the schema's messages quote the input."""
    if not isinstance(raw, str) or len(raw) > MAX_ANSWER_CHARS:
        msg = "the answer is not JSON text within the size limit"
        raise AnswerFormatError(msg)
    try:
        data = json.loads(raw, object_pairs_hook=_no_repeated_keys)
    except (ValueError, RecursionError):
        msg = "the answer is not valid JSON"
        raise AnswerFormatError(msg) from None
    return read_structured(data)


@dataclass(frozen=True)
class SentSource:
    """A passage as it was sent to the model, with the ids the server resolves its label to.

    ``chunk_id`` is a ``chunks.id``; ``version_id`` and ``document_id`` are the ULIDs of the
    version and document the passage is from (``vault.db.ids``). A packed source of several
    chunks (``vault.retrieval.packing``) is sent with the id of its first chunk, and the caller
    maps a quote span to the chunks it touches with the source's own parts."""

    chunk_id: int
    version_id: str
    document_id: str
    page_start: int
    page_end: int
    # The passage text exactly as the prompt held it; quote spans are offsets into it.
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        numbers = (self.chunk_id, self.page_start, self.page_end)
        # ``type(...) is int`` and not ``isinstance``: ``True`` is an ``int`` too.
        if any(type(number) is not int or number < 1 for number in numbers):
            msg = "a source's chunk id and pages must be whole numbers from 1"
            raise SourceError(msg)
        if not (is_ulid(self.version_id) and is_ulid(self.document_id)):
            msg = "a source's version and document ids must be ULIDs"
            raise SourceError(msg)
        if self.page_end < self.page_start:
            msg = "a source's last page is before its first"
            raise SourceError(msg)
        if type(self.text) is not str or not self.text.strip():
            msg = "a source's text must be a non-blank string"
            raise SourceError(msg)


class SourceMap:
    """The session's labels: ``S1`` for the first source given, ``S2`` for the next, and so on.

    The labels are made here, so their spelling is fixed by construction. A chunk given twice is
    refused: two labels for one passage would let one citation pass as two."""

    def __init__(self, sources: Sequence[SentSource]) -> None:
        if any(not isinstance(source, SentSource) for source in sources):
            msg = "every source must be a SentSource"
            raise SourceError(msg)
        if len({source.chunk_id for source in sources}) != len(sources):
            msg = "a chunk was given twice"
            raise SourceError(msg)
        self._by_label: Mapping[str, SentSource] = MappingProxyType(
            {f"{LABEL_PREFIX}{number}": source for number, source in enumerate(sources, start=1)}
        )

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self._by_label)

    def get(self, label: str) -> SentSource | None:
        """The source with exactly this label, or ``None``. No other spelling resolves."""
        return self._by_label.get(label) if type(label) is str else None

    def __len__(self) -> int:
        return len(self._by_label)


class FindingCode(StrEnum):
    INVENTED_LABEL = "invented_label"
    REPEATED_LABEL = "repeated_label"
    UNCITED_SEGMENT = "uncited_segment"
    MISSING_QUOTE = "missing_quote"
    QUOTE_NOT_FOUND = "quote_not_found"
    EMPTY_SEGMENT = "empty_segment"
    EMPTY_ANSWER = "empty_answer"
    FLAG_WITH_CLAIMS = "flag_with_claims"
    MISSING_NOTE = "missing_note"


MESSAGES: Final[Mapping[FindingCode, str]] = MappingProxyType(
    {
        FindingCode.INVENTED_LABEL: "The segment cites a source label that was not given.",
        FindingCode.REPEATED_LABEL: "The segment cites the same source label more than once.",
        FindingCode.UNCITED_SEGMENT: "The segment cites no source.",
        FindingCode.MISSING_QUOTE: "The segment cites sources but gives no quote.",
        FindingCode.QUOTE_NOT_FOUND: "The quote could not be verified in the cited source.",
        FindingCode.EMPTY_SEGMENT: "The segment has no text and was removed.",
        FindingCode.EMPTY_ANSWER: "The answer has no segment and does not say evidence is missing.",
        FindingCode.FLAG_WITH_CLAIMS: "The answer says evidence is missing but still makes claims.",
        FindingCode.MISSING_NOTE: "The answer says evidence is missing but not what is missing.",
    }
)


@dataclass(frozen=True)
class Finding:
    """One rule the answer broke. ``segment`` is the segment's position in the model's list (from
    0) and ``source`` the label's position in that segment's ``sources``; neither repeats text."""

    code: FindingCode
    segment: int | None = None
    source: int | None = None

    @property
    def message(self) -> str:
        return MESSAGES[self.code]


class SegmentStatus(StrEnum):
    SUPPORTED = "supported"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"
    UNCITED = "uncited"


@dataclass(frozen=True)
class Citation:
    """A label the server resolved, with the quote's span in the passage text if it was found."""

    label: str
    chunk_id: int
    version_id: str
    document_id: str
    page_start: int
    page_end: int
    quote_span: tuple[int, int] | None

    @property
    def verified(self) -> bool:
        return self.quote_span is not None


@dataclass(frozen=True)
class ValidatedSegment:
    text: str = field(repr=False)
    quote: str | None = field(repr=False)
    citations: tuple[Citation, ...]
    status: SegmentStatus


@dataclass(frozen=True)
class ValidatedAnswer:
    segments: tuple[ValidatedSegment, ...]
    insufficient_evidence: bool
    missing: str | None = field(repr=False)
    findings: tuple[Finding, ...]

    @property
    def cited_chunk_ids(self) -> frozenset[int]:
        """The ``SentSource.chunk_id`` of each source a kept segment cites. For a packed source
        of several chunks that is its first chunk only; ``vault.answers.ask`` resolves a quote to
        the chunks it touches (``AskResult.chunks_for``)."""
        return frozenset(
            citation.chunk_id for segment in self.segments for citation in segment.citations
        )


@cache
def _composes_with_previous() -> frozenset[str]:
    """The characters NFC can merge into the character before them (NFC_Quick_Check Maybe)."""
    seconds = {chr(cp) for cp in (*_HANGUL_V, *_HANGUL_T)}
    for cp in range(_DECOMPOSITION_END):
        composite = chr(cp)
        parts = unicodedata.decomposition(composite).split()
        if len(parts) != 2 or parts[0].startswith("<"):
            continue
        pair = chr(int(parts[0], 16)) + chr(int(parts[1], 16))
        # A composition exclusion decomposes but is never composed again.
        if unicodedata.normalize("NFC", pair) == composite:
            seconds.add(pair[1])
    return frozenset(seconds)


@cache
def _never_joins_previous(char: str) -> bool:
    """Whether NFC leaves text before ``char`` untouched whatever it is: ``char`` neither
    reorders with nor composes into what precedes it, and neither does the first character it
    decomposes to (NFC_Quick_Check Yes and canonical combining class 0)."""
    first = unicodedata.normalize("NFD", char)[0]
    return all(
        unicodedata.combining(c) == 0 and c not in _composes_with_previous() for c in (char, first)
    )


def _characters(text: str) -> list[tuple[int, int]]:
    """``text`` cut into whole characters as a reader sees them: extended grapheme clusters
    (UAX #29, :mod:`vault.answers.graphemes`: a letter with its marks, spacing vowel signs and
    variation selectors, an emoji with its modifiers and ZWJ sequence, a flag's two regional
    indicators, a Hangul syllable written in jamo), joined further wherever NFC would compose
    across the cut, so that every span normalises independently."""
    spans: list[tuple[int, int]] = []
    start = 0
    clusters = graphemes.boundaries(text)
    for index in range(1, len(text) + 1):
        if index < len(text):
            char = text[index]
            if not clusters[index]:
                continue
            if not _never_joins_previous(char):
                # A character that could compose or reorder (NFC_Quick_Check Maybe, or a mark,
                # which begins a cluster only after a control) starts a new span only if NFC
                # leaves the cut alone.
                before = text[start:index]
                if unicodedata.normalize("NFC", before + char) != unicodedata.normalize(
                    "NFC", before
                ) + unicodedata.normalize("NFC", char):
                    continue
        spans.append((start, index))
        start = index
    return spans if text else []


def _fold(char: str) -> str:
    if char in SINGLE_QUOTES:
        return "'"
    if char in DOUBLE_QUOTES:
        return '"'
    if char in DASHES:
        return "-"
    return char


@dataclass(frozen=True)
class _Folded:
    """Normalised text with, for each of its characters, the span of ``text`` it came from,
    whether it begins one of ``text``'s characters (where a match may start or end) and whether
    it stands for a run of white space."""

    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]
    boundary: tuple[bool, ...]
    space: tuple[bool, ...]


def _folded(text: str) -> _Folded:
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    boundary: list[bool] = []
    space: list[bool] = []
    for start, end in _characters(text):
        # A cluster made only of white space (one character, or CR LF, which UAX #29 keeps
        # together) is white space; one that carries a combining mark or any other character is
        # compared as it is, so a mark is never cut from the character it sits on.
        if all(char in WHITESPACE for char in text[start:end]):
            # A run counts as one space. A match never starts or ends on one (the quote's ends
            # are trimmed), so the span of a run is never needed.
            if not (space and space[-1]):
                chars.append(" ")
                starts.append(start)
                ends.append(end)
                boundary.append(True)
                space.append(True)
            continue
        first = True
        for char in unicodedata.normalize("NFC", text[start:end]):
            chars.append(_fold(char))
            starts.append(start)
            ends.append(end)
            boundary.append(first)
            space.append(False)
            first = False
    return _Folded("".join(chars), tuple(starts), tuple(ends), tuple(boundary), tuple(space))


def _trimmed(text: str) -> tuple[str, tuple[bool, ...]]:
    """The folded text with the white space runs at either end removed, and which of its
    characters stand for a run of white space."""
    folded = _folded(text)
    first, last = 0, len(folded.text)
    while first < last and folded.space[first]:
        first += 1
    while last > first and folded.space[last - 1]:
        last -= 1
    return folded.text[first:last], folded.space[first:last]


def normalise(text: str) -> str:
    """``text`` as quotes are compared: NFC, quotation marks and dashes folded, each run of white
    space as one space, and a run at either end removed. Nothing else is changed."""
    return _trimmed(text)[0]


def _find(quote: str, passage: _Folded) -> tuple[int, int] | None:
    needle, space = _trimmed(quote)
    if not needle:
        return None
    haystack = passage.text
    at = haystack.find(needle)
    while at != -1:
        after = at + len(needle)
        # A match starts and ends on whole characters, and a space in it stands for a run of
        # white space exactly where the quote's does (a space carrying a mark is not a run).
        if (
            passage.boundary[at]
            and (after == len(haystack) or passage.boundary[after])
            and passage.space[at:after] == space
        ):
            return passage.starts[at], passage.ends[after - 1]
        at = haystack.find(needle, at + 1)
    return None


def find_quote(quote: str, text: str) -> tuple[int, int] | None:
    """The span ``(start, end)`` in ``text`` of the first match of ``quote``, or ``None``."""
    return _find(quote, _folded(text))


def _status(*, cited: bool, invented: bool, verified: bool) -> SegmentStatus:
    """A segment's status, by precedence: no label at all is ``uncited``; any label that was not
    given makes it ``unsupported`` (plan section 11, ADR 0011), whatever its other labels show;
    otherwise a quote found in a cited passage makes it ``supported`` and anything less
    ``unverified``."""
    if not cited:
        return SegmentStatus.UNCITED
    if invented:
        return SegmentStatus.UNSUPPORTED
    return SegmentStatus.SUPPORTED if verified else SegmentStatus.UNVERIFIED


def validate_answer(answer: StructuredAnswer, sources: SourceMap) -> ValidatedAnswer:
    """``answer`` with its labels resolved through ``sources`` and its quotes checked."""
    findings: list[Finding] = []
    segments: list[ValidatedSegment] = []
    passages: dict[str, _Folded] = {}
    for position, segment in enumerate(answer.answer):
        if not segment.text.strip():
            findings.append(Finding(FindingCode.EMPTY_SEGMENT, position))
            continue
        # A ``null`` quote is no quote, as a blank one is.
        quote = segment.quote or ""
        quoted = bool(normalise(quote))
        citations: list[Citation] = []
        seen: set[str] = set()
        invented = False
        for index, label in enumerate(segment.sources):
            if label in seen:
                findings.append(Finding(FindingCode.REPEATED_LABEL, position, index))
                continue
            seen.add(label)
            source = sources.get(label)
            if source is None:
                findings.append(Finding(FindingCode.INVENTED_LABEL, position, index))
                invented = True
                continue
            span = None
            if quoted:
                if label not in passages:
                    passages[label] = _folded(source.text)
                span = _find(quote, passages[label])
                if span is None:
                    findings.append(Finding(FindingCode.QUOTE_NOT_FOUND, position, index))
            citations.append(
                Citation(
                    label=label,
                    chunk_id=source.chunk_id,
                    version_id=source.version_id,
                    document_id=source.document_id,
                    page_start=source.page_start,
                    page_end=source.page_end,
                    quote_span=span,
                )
            )
        # Findings are recorded whatever the status, and the status is decided by precedence
        # alone (``_status``), so no finding depends on which status won.
        if not segment.sources:
            findings.append(Finding(FindingCode.UNCITED_SEGMENT, position))
        if citations and not quoted:
            findings.append(Finding(FindingCode.MISSING_QUOTE, position))
        status = _status(
            cited=bool(segment.sources),
            invented=invented,
            verified=any(citation.verified for citation in citations),
        )
        segments.append(ValidatedSegment(segment.text, segment.quote, tuple(citations), status))
    if answer.insufficient_evidence:
        if segments:
            findings.append(Finding(FindingCode.FLAG_WITH_CLAIMS))
        if answer.missing is None or not answer.missing.strip():
            findings.append(Finding(FindingCode.MISSING_NOTE))
    elif not segments:
        findings.append(Finding(FindingCode.EMPTY_ANSWER))
    return ValidatedAnswer(
        segments=tuple(segments),
        insufficient_evidence=answer.insufficient_evidence or not segments,
        missing=answer.missing,
        findings=tuple(findings),
    )
