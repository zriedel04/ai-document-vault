"""Keyword search over the chunks' FTS5 index (plan section 8, step 3a, and the Search page).

The owner's query is never handed to FTS5 as query syntax. ``build_match`` reads it with a small
grammar of its own and writes the FTS5 expression itself, every piece of the owner's text inside a
quoted string, so no input can be an FTS5 operator, a column filter or a syntax error:

- Text between double quotes is a phrase: its words must appear together and in order. A quote
  that is never closed runs to the end of the query.
- Every other word is a term. All terms and phrases must match (AND); there is no OR or NOT
  for the owner to type, and ``OR``, ``NOT`` or ``NEAR`` are searched as words.
- A word ending in ``*`` is a prefix: ``partit*`` finds ``partitioning``.
- Course and other codes match with and without the space (plan section 8, step 2): ``CSE485``
  finds ``CSE485`` and ``CSE 485``, and ``CSE 485`` finds both too. A word of letters followed by
  a word of digits counts as one term. A code written with a mark between its letters and digits
  (``CSE-485``, ``CSE_485``, ``CSE/485``) is read as the code too, so it finds the joined form as
  well; the tokenizer splits at the mark, so a document that writes it so is found as ``CSE 485``
  is, and every written form of a code finds every other.
- Words with no letter or digit (``-``, ``&``) are dropped, because the tokenizer drops them too.
  A query left with no term matches nothing and is not run.

A query is at most ``MAX_QUERY_CHARS`` characters and ``MAX_TERMS`` terms (``QueryError``
otherwise), which bounds the work one request can ask of the index.

``search`` runs the expression against ``chunks_fts`` within the scope (plan section 8, step 1:
documents not in the trash, only the chunks of each document's current version, and the optional
type, label and import date filters, in the same statement, so the count, the ranking and the
pages all see the filtered set; a label keeps the documents that carry it or a label below it, by
the rule the document list uses, ``vault.db.labels.UNDER_LABEL_SQL``, and the import date is the
document's ``created_at``, when its bytes first entered the vault, between two inclusive instants
in UTC), ranks the
chunks by BM25 over the text, title and section path, and returns one result per document: its
best chunk, with a snippet of that chunk's text and the number of the document's chunks that match.
A document scores as its best chunk does, and results are sorted by that score, then by document
id, so the order is total and the same request on unchanged data gives the same page. Which chunk
is the best is decided by BM25 with the title's weight at 0: every chunk of a document carries the
same title, but BM25 normalises by the length of the whole row, so with the title counted a match
by name alone would pick whichever chunk is shortest. Counting, ranking and snippets are read from
one snapshot.

The snippet marks the matched words with markers made at random for each request, so a document's
own text can never pose as a highlight, and is returned as segments (``text``, ``match``) for the
page to render as text, never as markup. The name and the section titles are searched too, so
they come back marked the same way (``name_highlight``, ``section_highlight``): a result found
only by its file name or its section shows where the words matched. Its snippet is then the start
of the chosen chunk's text; for a match by name alone that is the document's first chunk, because
without the title every chunk scores 0 and equal scores fall back to the lowest chunk id.

Ask the Vault reads the same index differently (``rank_chunks``): its question is a sentence, so
``build_match(..., any_term=True)`` joins the terms with OR instead of AND, and the list holds
chunks, not documents, best BM25 first within the same scope (no filters yet), so one document
can hold several places until fusion caps them.

The query, the snippets and the names are the owner's: they go back to the page that asked and
are never logged, and no error here quotes them.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import BindParameter, Connection, DateTime, Engine, bindparam, text

from vault.db.engine import read_snapshot
from vault.db.labels import UNDER_LABEL_SQL, check_label
from vault.db.pages import is_storable
from vault.db.schema import TRASHED

MAX_QUERY_CHARS: Final = 500
MAX_TERMS: Final = 16
MAX_PAGE: Final = 50
DEFAULT_PAGE: Final = 20
# Tokens in a snippet (FTS5 allows at most 64).
SNIPPET_TOKENS: Final = 24
ELLIPSIS: Final = "…"

_LETTERS = re.compile(r"[^\W\d_]+")
_DIGITS = re.compile(r"\d+")
# Letters, then digits, joined or with marks between them (``CSE485``, ``CSE-485``, ``CSE_485``).
_CODE = re.compile(r"([^\W\d_]+)[\W_]*(\d+)")
# Leading and trailing characters that are neither letters nor digits ("CSE485," or "(485").
_EDGES = re.compile(r"^[\W_]+|[\W_]+$")


class QueryError(ValueError):
    """A query the search refuses. The message is fixed text and never quotes the query."""


def _string(value: str) -> str:
    """``value`` as an FTS5 string: inside double quotes, with each double quote doubled."""
    return '"' + value.replace('"', '""') + '"'


def _has_token(value: str) -> bool:
    return any(character.isalnum() for character in value)


def _split(query: str) -> list[tuple[str, bool]]:
    """The query as pieces of (text, is_phrase), in order."""
    pieces: list[tuple[str, bool]] = []
    rest = query
    while rest:
        before, quote, after = rest.partition('"')
        pieces.extend((word, False) for word in before.split())
        if not quote:
            break
        phrase, _closing, rest = after.partition('"')
        pieces.append((phrase, True))
    return pieces


def build_match(
    query: str,
    *,
    any_term: bool = False,
    max_chars: int = MAX_QUERY_CHARS,
    max_terms: int = MAX_TERMS,
) -> str | None:
    """The FTS5 expression for the owner's ``query``, or ``None`` when it has no term to match.

    Every term must match, unless ``any_term`` is set (Ask the Vault's question, which is a
    sentence and not a list of required words): then a chunk that matches any term is found and
    BM25 ranks the chunks that match more and rarer terms first. A term is read the same way in
    both modes (phrases, prefixes, codes). ``max_chars`` and ``max_terms`` are the bounds, the
    Search page's by default; they are constants of the caller, never values from a request.

    Raises ``QueryError`` for a query that is too long, has too many terms or holds text SQLite
    cannot store (a NUL, a lone surrogate)."""
    if not isinstance(query, str) or not is_storable(query):
        msg = "The query holds a character that is not text."
        raise QueryError(msg)
    if len(query) > max_chars:
        msg = f"The query is longer than {max_chars} characters."
        raise QueryError(msg)
    terms: list[str] = []
    pieces = [(value, phrase) for value, phrase in _split(query) if _has_token(value)]
    position = 0
    while position < len(pieces):
        value, phrase = pieces[position]
        position += 1
        if phrase:
            terms.append(_string(value))
            continue
        prefix = value.endswith("*")
        core = value.rstrip("*")  # still has a token: "*" is not a letter or digit
        if prefix:
            terms.append(_string(core) + "*")
            continue
        bare = _EDGES.sub("", core)
        code = _CODE.fullmatch(bare)
        if code:
            letters, digits = code.groups()
            # The word as typed stays one of the forms: SQLite's tables do not know every mark
            # Python does (a character newer than them is part of a token), and such a word must
            # still find itself.
            forms = dict.fromkeys((bare, letters + digits, f"{letters} {digits}"))
            terms.append("(" + " OR ".join(_string(form) for form in forms) + ")")
            continue
        if _LETTERS.fullmatch(bare) and position < len(pieces):
            following, following_phrase = pieces[position]
            digits = _EDGES.sub("", following)
            if not following_phrase and not following.endswith("*") and _DIGITS.fullmatch(digits):
                position += 1
                terms.append(
                    f"(({_string(bare)} AND {_string(digits)}) OR {_string(bare + digits)})"
                )
                continue
        terms.append(_string(core))
    if len(terms) > max_terms:
        msg = f"The query has more than {max_terms} terms."
        raise QueryError(msg)
    # Explicit AND: FTS5 has no implicit AND next to a parenthesised group. Every term is a
    # quoted string or a parenthesised group, so OR joins whole terms.
    return (" OR " if any_term else " AND ").join(terms) if terms else None


@dataclass(frozen=True)
class SearchQuery:
    query: str
    limit: int = DEFAULT_PAGE
    offset: int = 0
    # Canonical extensions (vault.ingest.validate.MIME_TYPES); empty means every type.
    extensions: tuple[str, ...] = ()
    # Only documents that carry this label or one below it (vault.db.labels); None for all.
    label_id: str | None = None
    # Only documents first imported (``documents.created_at``) at or after ``imported_from`` and
    # at or before ``imported_to``, both inclusive and naive UTC as the database stores times;
    # None leaves that side open.
    imported_from: datetime | None = None
    imported_to: datetime | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE:
            msg = f"limit must be between 1 and {MAX_PAGE}"
            raise ValueError(msg)
        if self.offset < 0:
            msg = "offset must not be negative"
            raise ValueError(msg)
        for bound in (self.imported_from, self.imported_to):
            if bound is not None and bound.tzinfo is not None:
                msg = "import bounds must be naive UTC, as the database stores times"
                raise ValueError(msg)
        if (
            self.imported_from is not None
            and self.imported_to is not None
            and self.imported_from > self.imported_to
        ):
            msg = "imported_from must not be after imported_to"
            raise ValueError(msg)


@dataclass(frozen=True)
class Segment:
    text: str
    match: bool


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    name: str | None
    extension: str
    chunk_id: int
    page: int
    char_start: int
    char_end: int
    section_path: tuple[str, ...]
    snippet: tuple[Segment, ...]
    matches: int  # the document's chunks that match, the best one included
    # The name and each section title with the matched words marked, because a query can match
    # them and not the text; without a match they are one unmarked segment (or none when empty).
    name_highlight: tuple[Segment, ...] = ()
    section_highlight: tuple[tuple[Segment, ...], ...] = ()


@dataclass(frozen=True)
class SearchPage:
    items: tuple[SearchHit, ...]
    total: int  # documents that match


# Chunks that match, in scope, each with its rank in its document and the document's match count.
# Fixed text: the expression and the extensions are bound parameters.
_SCOPED = (
    """
WITH hits AS (
    SELECT
        rowid AS chunk_id,
        bm25(chunks_fts) AS score,
        bm25(chunks_fts, 1.0, 0.0, 1.0) AS passage_score
    FROM chunks_fts WHERE chunks_fts MATCH :match
),
scoped AS (
    SELECT
        hits.chunk_id, hits.score AS chunk_score, chunks.document_id, chunks.page_start,
        chunks.char_start,
        chunks.char_end, chunks.section_path, blobs.ext,
        min(hits.score) OVER (PARTITION BY chunks.document_id) AS score,
        row_number() OVER (
            PARTITION BY chunks.document_id ORDER BY hits.passage_score, hits.chunk_id
        ) AS rank_in_document,
        count(*) OVER (PARTITION BY chunks.document_id) AS matches
    FROM hits
    JOIN chunks ON chunks.id = hits.chunk_id
    JOIN documents ON documents.id = chunks.document_id
        AND documents.current_version_id = chunks.version_id
    JOIN document_versions ON document_versions.id = chunks.version_id
    JOIN blobs ON blobs.sha256 = document_versions.blob_sha256
    WHERE documents.state != :trashed AND (:any_type OR blobs.ext IN :extensions)
        AND (:any_label OR chunks.document_id IN ("""
    + UNDER_LABEL_SQL
    + """))
        AND (:imported_from IS NULL OR documents.created_at >= :imported_from)
        AND (:imported_to IS NULL OR documents.created_at <= :imported_to)
)
"""
)
# Both are constants joined with constants; every value is a bound parameter.
_COUNT_SQL = _SCOPED + "SELECT count(*) FROM scoped WHERE rank_in_document = 1"  # noqa: S608
_PAGE_SQL = (
    _SCOPED  # noqa: S608
    + """SELECT scoped.*, chunks_fts.title AS name
    FROM scoped JOIN chunks_fts ON chunks_fts.rowid = scoped.chunk_id
    WHERE rank_in_document = 1
    ORDER BY score, document_id
    LIMIT :limit OFFSET :offset"""
)
# The bounds go through the column's own type, so they are written in the form the stored times
# have (SQLite keeps them as text in one fixed-width format, compared as text).
_SCOPE_PARAMS: Final[tuple[BindParameter[Any], ...]] = (
    bindparam("extensions", expanding=True),
    bindparam("imported_from", type_=DateTime()),
    bindparam("imported_to", type_=DateTime()),
)
# Ask the Vault's keyword list: every chunk in scope, best BM25 first (title and section path
# counted, as for the document's score), ties by chunk id, with the document's name.
_RANK_SQL = (
    _SCOPED  # noqa: S608
    + """SELECT scoped.chunk_id, scoped.document_id, chunks_fts.title AS name
    FROM scoped JOIN chunks_fts ON chunks_fts.rowid = scoped.chunk_id
    ORDER BY scoped.chunk_score, scoped.chunk_id
    LIMIT :limit"""
)
_COUNT = text(_COUNT_SQL).bindparams(*_SCOPE_PARAMS)
_RANK = text(_RANK_SQL).bindparams(*_SCOPE_PARAMS)
_PAGE = text(_PAGE_SQL).bindparams(*_SCOPE_PARAMS)
_SNIPPETS = text(
    "SELECT rowid AS chunk_id,"
    " snippet(chunks_fts, 0, :open, :close, :ellipsis, :tokens) AS snippet,"
    " highlight(chunks_fts, 1, :open, :close) AS title,"
    " highlight(chunks_fts, 2, :open, :close) AS section_path"
    " FROM chunks_fts WHERE chunks_fts MATCH :match AND rowid IN :chunk_ids"
).bindparams(bindparam("chunk_ids", expanding=True))


def _segments(marked: str, open_mark: str, close_mark: str) -> tuple[Segment, ...]:
    """The snippet FTS5 marked with ``open_mark`` and ``close_mark``, as plain and matched text."""
    segments: list[Segment] = []
    rest = marked
    while rest:
        before, opened, after = rest.partition(open_mark)
        if before:
            segments.append(Segment(before, match=False))
        if not opened:
            break
        matched, _closed, rest = after.partition(close_mark)
        if matched:
            segments.append(Segment(matched, match=True))
    return tuple(segments)


def _section_path(stored: str) -> tuple[str, ...]:
    """The stored JSON list of titles. The database can be edited by hand, so a value that is not
    a list of strings is a ``ValueError`` with fixed text, never a response that would fail later
    with the value in its message."""
    titles = json.loads(stored)
    if not isinstance(titles, list) or not all(isinstance(title, str) for title in titles):
        msg = "A stored section path is not a list of titles."
        raise ValueError(msg)
    return tuple(titles)


def search(engine: Engine, query: SearchQuery) -> SearchPage:
    """One page of the documents that match ``query``, best first (see the module docstring).

    Raises ``QueryError`` for a query ``build_match`` refuses, and
    ``vault.db.labels.UnknownLabelError`` when ``query.label_id`` names no label (also when the
    query has nothing to match, so a filter on a deleted label is never a silent empty page)."""
    match = build_match(query.query)
    if match is None:
        if query.label_id is not None:
            with read_snapshot(engine) as connection:
                check_label(connection, query.label_id)
        return SearchPage(items=(), total=0)
    scope = {
        "match": match,
        "trashed": TRASHED,
        "any_type": not query.extensions,
        # An empty IN list is not valid SQL everywhere; the flag above makes the value unused.
        "extensions": list(query.extensions) or [""],
        "any_label": query.label_id is None,
        "label_id": query.label_id,
        "imported_from": query.imported_from,
        "imported_to": query.imported_to,
    }
    # A random marker per request: no text in a document can contain it by chance or by design.
    nonce = secrets.token_hex(16)
    open_mark, close_mark = f"{nonce}", f"{nonce}"
    with read_snapshot(engine) as connection:
        if query.label_id is not None:
            check_label(connection, query.label_id)
        total: int = connection.execute(_COUNT, scope).scalar_one()
        rows = connection.execute(
            _PAGE, {**scope, "limit": query.limit, "offset": query.offset}
        ).all()
        marked: dict[int, Any] = {}
        if rows:
            marked = {
                row.chunk_id: row
                for row in connection.execute(
                    _SNIPPETS,
                    {
                        "match": match,
                        "open": open_mark,
                        "close": close_mark,
                        "ellipsis": ELLIPSIS,
                        "tokens": SNIPPET_TOKENS,
                        "chunk_ids": [row.chunk_id for row in rows],
                    },
                )
            }
    items = []
    for row in rows:
        section_path = _section_path(row.section_path)
        highlights = marked.get(row.chunk_id)
        snippet = _segments(highlights.snippet, open_mark, close_mark) if highlights else ()
        name = row.name or ""
        name_highlight = _segments(highlights.title, open_mark, close_mark) if highlights else ()
        if _plain(name_highlight) != name:
            name_highlight = _segments(name, open_mark, close_mark)
        section_highlight = (
            _marked_titles(highlights.section_path, section_path, open_mark, close_mark)
            if highlights
            else None
        ) or tuple(_segments(title, open_mark, close_mark) for title in section_path)
        items.append(
            SearchHit(
                document_id=row.document_id,
                name=name or None,
                extension=row.ext,
                chunk_id=row.chunk_id,
                page=row.page_start,
                char_start=row.char_start,
                char_end=row.char_end,
                section_path=section_path,
                snippet=snippet,
                matches=row.matches,
                name_highlight=name_highlight,
                section_highlight=section_highlight,
            )
        )
    return SearchPage(items=tuple(items), total=total)


@dataclass(frozen=True)
class RankedChunk:
    """One place in Ask the Vault's keyword list: a chunk, its document and the document's name
    (the keyword index's title, the library's name rule; ``None`` when it has none)."""

    chunk_id: int
    document_id: str
    name: str | None = field(repr=False)


def rank_chunks(connection: Connection, match: str, limit: int) -> tuple[RankedChunk, ...]:
    """The first ``limit`` chunks that ``match`` (a :func:`build_match` expression) finds, best
    first, within the scope of every search: documents not in the trash, only the chunks of
    each document's current version. Unlike :func:`search` it lists chunks, not documents, so
    one document can hold several places (Ask caps them per document after fusion).

    ``connection`` is the caller's snapshot, so the chunks it loads next agree with the list."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        msg = "limit must be a whole number from 1"
        raise ValueError(msg)
    scope = {
        "match": match,
        "trashed": TRASHED,
        "any_type": True,
        "extensions": [""],
        "any_label": True,
        "label_id": None,
        "imported_from": None,
        "imported_to": None,
        "limit": limit,
    }
    return tuple(
        RankedChunk(chunk_id=row.chunk_id, document_id=row.document_id, name=row.name)
        for row in connection.execute(_RANK, scope)
    )


def _plain(segments: tuple[Segment, ...]) -> str:
    return "".join(segment.text for segment in segments)


def _marked_titles(
    marked: str, titles: tuple[str, ...], open_mark: str, close_mark: str
) -> tuple[tuple[Segment, ...], ...] | None:
    """The section titles as FTS5 marked them in the stored JSON list, or ``None`` when the
    marked list does not read back as exactly ``titles`` once the marks are taken out (the marks
    sit between tokens, inside the JSON strings, so this does not happen with the chunker's own
    titles), so a mark can never change what a title says."""
    try:
        parsed = json.loads(marked)
    except ValueError:
        return None
    if not isinstance(parsed, list) or not all(isinstance(title, str) for title in parsed):
        return None
    result = tuple(_segments(title, open_mark, close_mark) for title in parsed)
    if tuple(_plain(title) for title in result) != titles:
        return None
    return result
