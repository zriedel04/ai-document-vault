"""The chunk repository: the retrieval chunks of each document version (plan section 6, ``chunks``).

``replace_chunks`` stores a chunker's output as the only chunks of a version, in one transaction:
a reader sees the old set or the new one, never a mixture, and the keyword index ``chunks_fts``
follows in the same transaction (triggers, migration ``0005``).

What is stored is checked first, against the rules of the chunker that made it and against the
version's pages as they are stored at that moment, inside the same transaction: chunks are
numbered 0, 1, 2 ... without a gap, each lies on one page that exists, its span is inside that
page's text and its text is exactly that span, without white space at either end, its token count
is the chunker's count of that text, and its section path is a list of titles in the chunker's
form. So chunks made from pages that were re-extracted in the meantime are refused rather than
stored with offsets into text that is gone. The section path is checked for its form, not against
the headings: only running the chunker again could tell a wrong path from a right one.

Chunk text and section titles are the document's own words. They are never logged, and no error
here quotes them: messages name a chunk by its index and a page by its number.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import Connection, Engine, delete, insert, select

from vault.db import ids
from vault.db.pages import VersionNotFoundError, is_int, is_storable
from vault.db.schema import chunks, document_versions, pages
from vault.ingest.chunking import structural_v1
from vault.ingest.chunking.base import Chunk

# Every chunker the application has, with its token rule and its section title rule.
CHUNKERS = {
    structural_v1.CHUNKER_VERSION: (structural_v1.count_tokens, structural_v1.section_title),
}


def check_chunks(chunks_in: Sequence[Chunk], chunker_version: str) -> tuple[Chunk, ...]:
    """The chunks, if they follow the chunker's rules on their own (the pages are checked when
    they are stored); a ``ValueError`` naming the first break if not."""
    if not isinstance(chunker_version, str) or chunker_version not in CHUNKERS:
        msg = "The chunker version is not one this application has."
        raise ValueError(msg)
    count_tokens, section_title = CHUNKERS[chunker_version]
    checked = tuple(chunks_in)
    for position, chunk in enumerate(checked):
        where = f"Chunk {position}"
        if not isinstance(chunk, Chunk) or not is_int(chunk.index) or chunk.index != position:
            msg = f"{where} is missing or out of order: chunks are numbered 0, 1, 2 ..."
            raise ValueError(msg)
        numbers = (chunk.page_start, chunk.page_end, chunk.char_start, chunk.char_end)
        if not all(is_int(number) for number in numbers) or not is_int(chunk.token_count):
            msg = f"{where} has a page, an offset or a token count that is not a whole number."
            raise ValueError(msg)
        if chunk.page_start < 1 or chunk.page_end != chunk.page_start:
            msg = f"{where} must lie on one page, numbered from 1."
            raise ValueError(msg)
        if not 0 <= chunk.char_start < chunk.char_end:
            msg = f"{where} has an empty span or one that starts before its page."
            raise ValueError(msg)
        if (
            not isinstance(chunk.text, str)
            or not is_storable(chunk.text)
            or len(chunk.text) != chunk.char_end - chunk.char_start
            or chunk.text.strip() != chunk.text
        ):
            msg = f"{where} has text that is not its span, or white space at an end."
            raise ValueError(msg)
        if chunk.token_count != count_tokens(chunk.text):
            msg = f"{where} has a token count that is not the chunker's count of its text."
            raise ValueError(msg)
        if not isinstance(chunk.section_path, tuple) or not all(
            isinstance(title, str) and is_storable(title) and section_title(title) == title
            for title in chunk.section_path
        ):
            msg = f"{where} has a section path that is not a tuple of titles in the chunker's form."
            raise ValueError(msg)
    return checked


def replace_chunks(
    engine: Engine, version_id: str, chunks_in: Sequence[Chunk], chunker_version: str
) -> None:
    """Store ``chunks_in`` as the only chunks of the version, in one transaction.

    Raises ``ValueError`` for chunks that break the rules or do not match the version's stored
    pages, and ``VersionNotFoundError`` for an id that names no version; in both cases nothing
    changes. An empty sequence removes the version's chunks (pages with no visible text).
    """
    checked = check_chunks(chunks_in, chunker_version)
    if not ids.is_ulid(version_id):
        raise VersionNotFoundError
    with engine.begin() as connection:
        # The first statement writes, so the transaction holds SQLite's write lock from here on:
        # neither the version nor its pages can change before the insert.
        write_chunks(connection, version_id, checked, chunker_version)


def write_chunks(
    connection: Connection, version_id: str, checked: Sequence[Chunk], chunker_version: str
) -> None:
    """``replace_chunks`` in the caller's transaction, for chunks ``check_chunks`` has passed. The
    caller holds the write lock, or makes this its first statement, which takes it."""
    connection.execute(delete(chunks).where(chunks.c.version_id == version_id))
    document_id = connection.execute(
        select(document_versions.c.document_id).where(document_versions.c.id == version_id)
    ).scalar_one_or_none()
    if document_id is None:
        raise VersionNotFoundError
    page_texts: dict[int, str] = {
        row.page_no: row.text
        for row in connection.execute(
            select(pages.c.page_no, pages.c.text).where(pages.c.version_id == version_id)
        )
    }
    for chunk in checked:
        page_text = page_texts.get(chunk.page_start)
        if page_text is None:
            msg = f"Chunk {chunk.index} names page {chunk.page_start}, which is not stored."
            raise ValueError(msg)
        if page_text[chunk.char_start : chunk.char_end] != chunk.text:
            msg = f"Chunk {chunk.index} is not the text of page {chunk.page_start} at its span."
            raise ValueError(msg)
    if checked:
        connection.execute(
            insert(chunks),
            [
                {
                    "version_id": version_id,
                    "document_id": document_id,
                    "chunk_index": chunk.index,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "section_path": json.dumps(list(chunk.section_path), ensure_ascii=False),
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "text": chunk.text,
                    "token_count": chunk.token_count,
                    "chunker_version": chunker_version,
                }
                for chunk in checked
            ],
        )
