"""The page repository: the extracted text of each document version (plan section 6, ``pages``).

An extractor reads one version's blob into pages, and ``replace_pages`` stores them: all pages of
that version are replaced in one transaction, so a reader sees the old set or the new one, never
a mixture, and a second extraction (a new extractor version) leaves no page of the first behind.
``read_pages`` gives them back in order, and ``read_current_pages`` gives the text view one window
of a document's current version at a time.

What is stored is checked on the way in and again on the way out, because the database can be
edited by hand: page numbers run 1, 2, 3 ... without a gap, a heading lies inside its page's text,
in order and not overlapping another, the extractor is a name and a version (``text-1``), and the
encoding is the canonical name of a Python codec (``utf-8``, ``cp1252``).

Page text and heading titles are the document's own words. They are never logged, and no error
here quotes them: messages name a page by its number and a heading by its position.
"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import Connection, Engine, delete, func, insert, select

from vault.db import ids
from vault.db.engine import read_snapshot
from vault.db.schema import document_versions, documents, pages

# The application's rule for an extractor's name: lower-case words joined by hyphens, then the
# version, a whole number from 1 (the database checks a looser form of the same rule).
EXTRACTOR: Final = re.compile(r"[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*-[1-9][0-9]{0,5}")
ENCODING: Final = re.compile(r"[a-z][a-z0-9_-]{0,39}")
MAX_EXTRACTOR_LENGTH: Final = 40
HEADING_LEVELS: Final = range(1, 7)


class VersionNotFoundError(LookupError):
    """No document version has the given id."""

    def __init__(self) -> None:
        super().__init__("There is no such document version.")


class PageDataError(ValueError):
    """Stored pages that break the rules above (a hand edit, or a bug). Messages are fixed text."""


@dataclass(frozen=True)
class PageHeading:
    """A heading: its level (1 is the top), its title, and the span ``text[start:end]`` it takes."""

    level: int
    title: str
    start: int
    end: int


@dataclass(frozen=True)
class PageText:
    """One page: its number (from 1), its text and the headings in it."""

    number: int
    text: str
    headings: tuple[PageHeading, ...] = ()


@dataclass(frozen=True)
class StoredPages:
    """Every page of a version, with the extractor that read them and the encoding it used."""

    extractor: str
    encoding: str | None
    pages: tuple[PageText, ...]


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_storable(value: str) -> bool:
    """Text SQLite stores and measures as Python does: no NUL, no lone surrogate."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_extractor(extractor: object) -> str:
    if (
        not isinstance(extractor, str)
        or len(extractor) > MAX_EXTRACTOR_LENGTH
        or EXTRACTOR.fullmatch(extractor) is None
    ):
        msg = "The extractor must be a name and a version, such as text-1."
        raise ValueError(msg)
    return extractor


def check_encoding(encoding: object) -> str | None:
    """``None``, or the canonical name of a Python codec (the name ``codecs.lookup`` gives)."""
    if encoding is None:
        return None
    if isinstance(encoding, str) and ENCODING.fullmatch(encoding):
        try:
            canonical = codecs.lookup(encoding).name
        except LookupError:
            canonical = None
        if canonical == encoding:
            return encoding
    msg = "The encoding must be the canonical name of a Python codec, such as utf-8."
    raise ValueError(msg)


def check_pages(pages_in: Sequence[PageText]) -> tuple[PageText, ...]:
    """The pages, if they follow the rules above; a ``ValueError`` naming the first break if not."""
    checked = tuple(pages_in)
    if not checked:
        msg = "A version has at least one page (an empty file is one empty page)."
        raise ValueError(msg)
    return _check_run(checked, first=1)


def _check_run(checked: tuple[PageText, ...], first: int) -> tuple[PageText, ...]:
    """Pages numbered ``first``, ``first + 1`` ... each with storable text and valid headings."""
    for position, page in enumerate(checked, start=first):
        if not isinstance(page, PageText) or not is_int(page.number) or page.number != position:
            msg = f"Page {position} is missing or out of order: pages are numbered 1, 2, 3 ..."
            raise ValueError(msg)
        if not isinstance(page.text, str) or not is_storable(page.text):
            msg = f"The text of page {position} holds a NUL or a character that is not Unicode."
            raise ValueError(msg)
        _check_headings(page)
    return checked


def _check_headings(page: PageText) -> None:
    previous_end = 0
    if not isinstance(page.headings, tuple):
        msg = f"The headings of page {page.number} must be a tuple."
        raise ValueError(msg)
    for position, heading in enumerate(page.headings, start=1):
        where = f"Heading {position} of page {page.number}"
        if not isinstance(heading, PageHeading):
            msg = f"{where} is not a heading."
            raise ValueError(msg)
        if not is_int(heading.level) or heading.level not in HEADING_LEVELS:
            msg = f"{where} has a level outside 1 to 6."
            raise ValueError(msg)
        if not isinstance(heading.title, str) or not is_storable(heading.title):
            msg = f"{where} has a title that is not storable text."
            raise ValueError(msg)
        if not (is_int(heading.start) and is_int(heading.end)):
            msg = f"{where} has offsets that are not whole numbers."
            raise ValueError(msg)
        if not previous_end <= heading.start < heading.end <= len(page.text):
            msg = f"{where} is outside its page, empty, or overlaps the heading before it."
            raise ValueError(msg)
        previous_end = heading.end


def replace_pages(
    engine: Engine,
    version_id: str,
    pages_in: Sequence[PageText],
    extractor: str,
    encoding: str | None,
) -> None:
    """Store ``pages_in`` as the only pages of the version, in one transaction.

    Raises ``ValueError`` for pages, an extractor or an encoding that break the rules, before
    anything is written, and ``VersionNotFoundError`` for an id that names no version. The
    version's chunks go with its old pages (a foreign-key cascade): they point at the old text.
    """
    checked = check_pages(pages_in)
    check_extractor(extractor)
    check_encoding(encoding)
    if not ids.is_ulid(version_id):
        raise VersionNotFoundError
    rows = [
        {
            "version_id": version_id,
            "page_no": page.number,
            "text": page.text,
            "char_count": len(page.text),
            "headings": json.dumps(
                [[h.level, h.title, h.start, h.end] for h in page.headings], ensure_ascii=False
            ),
            "extractor": extractor,
            "encoding": encoding,
        }
        for page in checked
    ]
    with engine.begin() as connection:
        # The first statement writes, so the transaction holds SQLite's write lock from here on
        # and the version cannot disappear between the check and the insert.
        connection.execute(delete(pages).where(pages.c.version_id == version_id))
        found = connection.execute(
            select(document_versions.c.id).where(document_versions.c.id == version_id)
        ).first()
        if found is None:
            raise VersionNotFoundError
        connection.execute(insert(pages), rows)


def read_pages(engine: Engine, version_id: str) -> StoredPages | None:
    """Every page of the version in order, or ``None`` if it has none (not extracted yet, or no
    such version). Raises ``PageDataError`` if what is stored breaks the rules."""
    if not ids.is_ulid(version_id):
        return None
    with read_snapshot(engine) as connection:
        return pages_of(connection, version_id)


def pages_of(connection: Connection, version_id: str) -> StoredPages | None:
    """``read_pages`` in the caller's transaction, so what it reads can be acted on before anyone
    else writes (the chunk stage compares it with what it chunked under the write lock)."""
    rows = connection.execute(
        select(pages).where(pages.c.version_id == version_id).order_by(pages.c.page_no)
    ).all()
    if not rows:
        return None
    if len({(row.extractor, row.encoding) for row in rows}) != 1:
        msg = "The pages of one version name different extractors or encodings."
        raise PageDataError(msg)
    try:
        result = StoredPages(
            extractor=check_extractor(rows[0].extractor),
            encoding=check_encoding(rows[0].encoding),
            pages=check_pages(
                [PageText(row.page_no, row.text, _headings_from_json(row.headings)) for row in rows]
            ),
        )
    except PageDataError:
        raise
    except ValueError as error:
        raise PageDataError(str(error)) from None
    return result


def _headings_from_json(stored: object) -> tuple[PageHeading, ...]:
    """The headings in their stored form, ``[[level, title, start, end], ...]``, read strictly."""
    try:
        items = json.loads(stored) if isinstance(stored, str) else None
    except ValueError:
        items = None
    if not isinstance(items, list) or not all(
        isinstance(item, list) and len(item) == 4 for item in items
    ):
        msg = "Stored headings are not a list of [level, title, start, end]."
        raise PageDataError(msg)
    return tuple(PageHeading(*item) for item in items)


# The text view's window (``GET /api/documents/{id}/pages``): at most MAX_WINDOW pages, and no
# more text than MAX_WINDOW_CHARS, except that the first page of a window is always sent whole.
DEFAULT_WINDOW: Final = 20
MAX_WINDOW: Final = 100
MAX_WINDOW_CHARS: Final = 1_000_000


@dataclass(frozen=True)
class PageWindow:
    """Some pages of a document's current version, and how many pages that version has.

    ``total`` is 0 and ``extractor`` is ``None`` when the version has not been extracted yet.
    """

    version_id: str
    extractor: str | None
    total: int
    pages: tuple[PageText, ...]


def read_current_pages(
    engine: Engine,
    document_id: str,
    offset: int,
    limit: int = DEFAULT_WINDOW,
    max_chars: int = MAX_WINDOW_CHARS,
) -> PageWindow | None:
    """The pages of the document's current version after the first ``offset``, in order: at most
    ``limit`` of them, and only as many as keep the text before the last one under ``max_chars``
    characters (so the first is always included). ``None`` for an unknown document, in use or in
    the trash. A version holds the pages of its latest extraction only (``replace_pages``).

    Every read shares one snapshot, so the version, its count and its pages agree. Raises
    ``PageDataError`` if the stored pages break the rules, checked for the whole version (one
    extractor, pages 1 to ``total`` without a gap) and for each page sent.
    """
    if not 1 <= limit <= MAX_WINDOW or offset < 0 or max_chars < 1:
        msg = "The window must have 1 to 100 pages, a positive budget and a non-negative offset."
        raise ValueError(msg)
    if not ids.is_ulid(document_id):
        return None
    with read_snapshot(engine) as connection:
        version_id = connection.execute(
            select(documents.c.current_version_id).where(documents.c.id == document_id)
        ).scalar_one_or_none()
        if version_id is None:
            return None
        groups = connection.execute(
            select(
                pages.c.extractor,
                pages.c.encoding,
                func.count().label("count"),
                func.min(pages.c.page_no).label("first"),
                func.max(pages.c.page_no).label("last"),
            )
            .where(pages.c.version_id == version_id)
            .group_by(pages.c.extractor, pages.c.encoding)
        ).all()
        if not groups:
            return PageWindow(version_id=version_id, extractor=None, total=0, pages=())
        # The text before each page after the offset, from the counts alone, so only the texts of
        # the pages sent are selected.
        before = (
            select(
                pages.c.page_no,
                (
                    func.sum(pages.c.char_count).over(order_by=pages.c.page_no) - pages.c.char_count
                ).label("before"),
            )
            .where(pages.c.version_id == version_id, pages.c.page_no > offset)
            .subquery("before")
        )
        rows = connection.execute(
            select(pages.c.page_no, pages.c.text, pages.c.headings)
            .join(before, before.c.page_no == pages.c.page_no)
            .where(pages.c.version_id == version_id, before.c.before < max_chars)
            .order_by(pages.c.page_no)
            .limit(limit)
        ).all()
    if len(groups) != 1:
        msg = "The pages of one version name different extractors or encodings."
        raise PageDataError(msg)
    group = groups[0]
    if group.first != 1 or group.last != group.count:
        msg = "The pages of this version are not numbered 1, 2, 3 ... without a gap."
        raise PageDataError(msg)
    try:
        extractor = check_extractor(group.extractor)
        check_encoding(group.encoding)
        window = _check_run(
            tuple(PageText(r.page_no, r.text, _headings_from_json(r.headings)) for r in rows),
            first=offset + 1,
        )
    except PageDataError:
        raise
    except ValueError as error:
        raise PageDataError(str(error)) from None
    return PageWindow(
        version_id=version_id, extractor=extractor, total=int(group.count), pages=window
    )
