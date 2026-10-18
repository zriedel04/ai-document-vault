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
  a word of digits counts as one term.
- Words with no letter or digit (``-``, ``&``) are dropped, because the tokenizer drops them too.
  A query left with no term matches nothing and is not run.

A query is at most ``MAX_QUERY_CHARS`` characters and ``MAX_TERMS`` terms (``QueryError``
otherwise), which bounds the work one request can ask of the index.

``search`` runs the expression against ``chunks_fts`` within the scope (plan section 8, step 1:
documents not in the trash, and only the chunks of each document's current version), ranks the
chunks by BM25 over the text, title and section path, and returns one result per document: its
best chunk, with a snippet of that chunk's text and the number of the document's chunks that match.
Results are sorted by the best chunk's score, then by document id, so the order is total and the
same request on unchanged data gives the same page. Counting, ranking and snippets are read from
one snapshot.

The snippet marks the matched words with markers made at random for each request, so a document's
own text can never pose as a highlight, and is returned as segments (``text``, ``match``) for the
page to render as text, never as markup.

The query, the snippets and the names are the owner's: they go back to the page that asked and
are never logged, and no error here quotes them.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Engine, bindparam, text

from vault.db.engine import read_snapshot
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
_CODE = re.compile(r"([^\W\d_]+)(\d+)")
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


def build_match(query: str) -> str | None:
    """The FTS5 expression for the owner's ``query``, or ``None`` when it has no term to match.

    Raises ``QueryError`` for a query that is too long, has too many terms or holds text SQLite
    cannot store (a NUL, a lone surrogate)."""
    if not isinstance(query, str) or not is_storable(query):
        msg = "The query holds a character that is not text."
        raise QueryError(msg)
    if len(query) > MAX_QUERY_CHARS:
        msg = f"The query is longer than {MAX_QUERY_CHARS} characters."
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
            terms.append(f"({_string(bare)} OR {_string(f'{letters} {digits}')})")
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
    if len(terms) > MAX_TERMS:
        msg = f"The query has more than {MAX_TERMS} terms."
        raise QueryError(msg)
    # Explicit AND: FTS5 has no implicit AND next to a parenthesised group.
    return " AND ".join(terms) if terms else None


@dataclass(frozen=True)
class SearchQuery:
    query: str
    limit: int = DEFAULT_PAGE
    offset: int = 0
    # Canonical extensions (vault.ingest.validate.MIME_TYPES); empty means every type.
    extensions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE:
            msg = f"limit must be between 1 and {MAX_PAGE}"
            raise ValueError(msg)
        if self.offset < 0:
            msg = "offset must not be negative"
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


@dataclass(frozen=True)
class SearchPage:
    items: tuple[SearchHit, ...]
    total: int  # documents that match


# Chunks that match, in scope, each with its rank in its document and the document's match count.
# Fixed text: the expression and the extensions are bound parameters.
_SCOPED = """
WITH hits AS (
    SELECT rowid AS chunk_id, bm25(chunks_fts) AS score
    FROM chunks_fts WHERE chunks_fts MATCH :match
),
scoped AS (
    SELECT
        hits.chunk_id, hits.score, chunks.document_id, chunks.page_start, chunks.char_start,
        chunks.char_end, chunks.section_path, blobs.ext,
        row_number() OVER (
            PARTITION BY chunks.document_id ORDER BY hits.score, hits.chunk_id
        ) AS rank_in_document,
        count(*) OVER (PARTITION BY chunks.document_id) AS matches
    FROM hits
    JOIN chunks ON chunks.id = hits.chunk_id
    JOIN documents ON documents.id = chunks.document_id
        AND documents.current_version_id = chunks.version_id
    JOIN document_versions ON document_versions.id = chunks.version_id
    JOIN blobs ON blobs.sha256 = document_versions.blob_sha256
    WHERE documents.state != :trashed AND (:any_type OR blobs.ext IN :extensions)
)
"""
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
_COUNT = text(_COUNT_SQL).bindparams(bindparam("extensions", expanding=True))
_PAGE = text(_PAGE_SQL).bindparams(bindparam("extensions", expanding=True))
_SNIPPETS = text(
    "SELECT rowid AS chunk_id,"
    " snippet(chunks_fts, 0, :open, :close, :ellipsis, :tokens) AS snippet"
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

    Raises ``QueryError`` for a query ``build_match`` refuses."""
    match = build_match(query.query)
    if match is None:
        return SearchPage(items=(), total=0)
    scope = {
        "match": match,
        "trashed": TRASHED,
        "any_type": not query.extensions,
        # An empty IN list is not valid SQL everywhere; the flag above makes the value unused.
        "extensions": list(query.extensions) or [""],
    }
    # A random marker per request: no text in a document can contain it by chance or by design.
    nonce = secrets.token_hex(16)
    open_mark, close_mark = f"{nonce}", f"{nonce}"
    with read_snapshot(engine) as connection:
        total: int = connection.execute(_COUNT, scope).scalar_one()
        rows = connection.execute(
            _PAGE, {**scope, "limit": query.limit, "offset": query.offset}
        ).all()
        snippets: dict[int, str] = {}
        if rows:
            snippets = {
                row.chunk_id: row.snippet
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
    items = tuple(
        SearchHit(
            document_id=row.document_id,
            name=row.name or None,
            extension=row.ext,
            chunk_id=row.chunk_id,
            page=row.page_start,
            char_start=row.char_start,
            char_end=row.char_end,
            section_path=_section_path(row.section_path),
            snippet=_segments(snippets.get(row.chunk_id, ""), open_mark, close_mark),
            matches=row.matches,
        )
        for row in rows
    )
    return SearchPage(items=items, total=total)
