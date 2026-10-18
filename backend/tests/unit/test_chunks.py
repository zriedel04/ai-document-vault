"""The chunks table, its FTS5 index and the chunk repository, on synthetic text."""

from __future__ import annotations

import dataclasses
import json
import random
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, delete, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from tests.unit.test_documents_schema import NOW, add_document, source_row
from vault.db import ids
from vault.db.chunks import check_chunks, replace_chunks
from vault.db.documents import ListQuery, get_document, list_documents
from vault.db.engine import create_vault_engine
from vault.db.migrate import alembic_config, upgrade_to_latest
from vault.db.pages import PageHeading, PageText, VersionNotFoundError, replace_pages
from vault.db.schema import (
    FTS_TABLES,
    chunks,
    document_versions,
    documents,
    import_sources,
    pages,
)
from vault.ingest.chunking.base import Chunk
from vault.ingest.chunking.structural_v1 import CHUNKER_VERSION, chunk_pages

CAFE = "café"
CJK = "履歴書"
CANARY = "canary-chunk-text-xyzzy"
VERSION = CHUNKER_VERSION


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


def course_pages() -> list[PageText]:
    first = (
        "# Software Testing\n"
        "This syllabus covers equivalent partitioning and boundary values.\n\n"
        "## Grading\n"
        "CSE 485 meets on Tuesdays. Partitions of the input are tested at the edges.\n"
    )
    grading = first.index("## Grading")
    headings = (
        PageHeading(1, "Software Testing", 0, 18),
        PageHeading(2, "Grading", grading, grading + 10),
    )
    second = f"Notes on {CAFE} and {CJK}. The equivalent of partitioning. 485 CSE. CSE 486.\n"
    return [PageText(1, first, headings), PageText(2, second), PageText(3, "")]


@pytest.fixture
def version_id(engine: Engine) -> str:
    version = add_document(engine)[1]
    replace_pages(engine, version, course_pages(), "text-1", "utf-8")
    return version


def store(engine: Engine, version_id: str) -> tuple[Chunk, ...]:
    stored = read_pages_back(engine, version_id)
    result = chunk_pages(stored)
    replace_chunks(engine, version_id, result, VERSION)
    return result


def read_pages_back(engine: Engine, version_id: str) -> list[PageText]:
    from vault.db.pages import read_pages

    stored = read_pages(engine, version_id)
    assert stored is not None
    return list(stored.pages)


def search(engine: Engine, query: str) -> list[int]:
    """The chunk indexes whose index row matches an FTS5 query, best first."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT chunks.chunk_index FROM chunks_fts"
                " JOIN chunks ON chunks.id = chunks_fts.rowid"
                " WHERE chunks_fts MATCH :query ORDER BY bm25(chunks_fts), chunks.id"
            ),
            {"query": query},
        ).all()
    return [row[0] for row in rows]


def index_rows(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text("SELECT rowid, text, title, section_path FROM chunks_fts ORDER BY rowid")
            )
        ]


def assert_index_in_step(engine: Engine) -> None:
    """The FTS5 index holds exactly one row per chunk, with the chunk's text and section path, and
    passes FTS5's own integrity check."""
    with engine.begin() as connection:
        expected = [
            (row.id, row.text, row.section_path)
            for row in connection.execute(select(chunks).order_by(chunks.c.id))
        ]
        connection.execute(text("INSERT INTO chunks_fts (chunks_fts) VALUES ('integrity-check')"))
    assert [(row[0], row[1], row[3]) for row in index_rows(engine)] == expected


def chunk_row(version_id: str, document_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "version_id": version_id,
        "document_id": document_id,
        "chunk_index": 0,
        "page_start": 1,
        "page_end": 1,
        "section_path": "[]",
        "char_start": 0,
        "char_end": 5,
        "text": "hello",
        "token_count": 2,
        "chunker_version": VERSION,
    } | changes


def document_of(engine: Engine, version_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                select(document_versions.c.document_id).where(document_versions.c.id == version_id)
            ).scalar_one()
        )


# --- storing -------------------------------------------------------------------------------------


def test_chunks_are_stored_with_their_pages_offsets_and_section_paths(
    engine: Engine, version_id: str
) -> None:
    made = store(engine, version_id)
    page_texts = {page.number: page.text for page in course_pages()}
    with engine.connect() as connection:
        rows = connection.execute(select(chunks).order_by(chunks.c.chunk_index)).all()
    assert len(rows) == len(made) == 3
    for row, chunk in zip(rows, made, strict=True):
        assert row.document_id == document_of(engine, version_id)
        assert row.text == page_texts[row.page_start][row.char_start : row.char_end] == chunk.text
        assert json.loads(row.section_path) == list(chunk.section_path)
        assert row.chunker_version == VERSION
        assert row.token_count == chunk.token_count
    assert [json.loads(row.section_path) for row in rows] == [
        ["Software Testing"],
        ["Software Testing", "Grading"],
        ["Software Testing", "Grading"],
    ]
    assert_index_in_step(engine)


def test_replacing_chunks_keeps_the_index_in_step(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    first_ids = {row[0] for row in index_rows(engine)}
    pages_in = course_pages()
    pages_in[1] = PageText(2, "Only one new line about mutation testing.")
    replace_pages(engine, version_id, pages_in, "text-1", "utf-8")
    store(engine, version_id)

    assert_index_in_step(engine)
    assert first_ids.isdisjoint(row[0] for row in index_rows(engine))
    assert search(engine, "mutation") == [2]
    assert search(engine, CAFE) == []


def test_an_empty_list_removes_the_versions_chunks(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    replace_chunks(engine, version_id, (), VERSION)
    assert index_rows(engine) == []
    assert_index_in_step(engine)


def test_another_versions_chunks_are_left_alone(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    other = add_document(engine)[1]
    replace_pages(engine, other, [PageText(1, "Other text.")], "text-1", None)
    store(engine, other)
    store(engine, version_id)
    assert search(engine, "other") == [0]
    assert len(index_rows(engine)) == 4
    assert_index_in_step(engine)


def test_chunk_ids_are_never_reused(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    highest = max(row[0] for row in index_rows(engine))
    replace_chunks(engine, version_id, (), VERSION)
    store(engine, version_id)
    assert min(row[0] for row in index_rows(engine)) > highest


# --- what the repository refuses -----------------------------------------------------------------


def test_an_id_that_names_no_version_is_refused(engine: Engine) -> None:
    for version in ("not-a-ulid", ids.new_ulid()):
        with pytest.raises(VersionNotFoundError):
            replace_chunks(engine, version, (), VERSION)


def test_chunks_made_from_other_pages_are_refused_and_nothing_changes(
    engine: Engine, version_id: str
) -> None:
    made = store(engine, version_id)
    before = index_rows(engine)
    stale = chunk_pages([PageText(1, f"Different words {CANARY}.\n"), PageText(2, "x")])
    too_far = dataclasses.replace(made[-1], page_start=4, page_end=4)
    for wrong in (stale, (*made[:-1], too_far)):
        with pytest.raises(ValueError, match=r"Chunk \d") as caught:
            replace_chunks(engine, version_id, wrong, VERSION)
        assert CANARY not in str(caught.value)
    assert index_rows(engine) == before


def test_chunks_of_a_version_with_no_pages_are_refused(engine: Engine) -> None:
    version = add_document(engine)[1]
    with pytest.raises(ValueError, match="not stored"):
        replace_chunks(engine, version, chunk_pages([PageText(1, "Text.")]), VERSION)


def good_chunk() -> Chunk:
    return chunk_pages([PageText(1, f"{CANARY} text")])[0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"index": 1}, "out of order"),
        ({"index": True}, "out of order"),
        ({"page_start": 0, "page_end": 0}, "one page"),
        ({"page_end": 2}, "one page"),
        ({"page_start": 1.0}, "whole number"),
        ({"token_count": "3"}, "whole number"),
        ({"char_start": 5, "char_end": 5}, "empty span"),
        ({"char_start": -1}, "empty span"),
        ({"char_end": 99}, "not its span"),
        ({"text": f" {CANARY} tex", "char_end": len(CANARY) + 5}, "white space"),
        ({"text": f"{CANARY}\x00text"}, "not its span"),
        ({"token_count": 99}, "token count"),
        ({"section_path": ["A"]}, "section path"),
        ({"section_path": ("A  B",)}, "section path"),
        ({"section_path": ("A\tB",)}, "section path"),
        ({"section_path": (1,)}, "section path"),
    ],
)
def test_chunks_that_break_the_rules_are_refused_before_anything_is_written(
    engine: Engine, change: dict[str, Any], message: str
) -> None:
    wrong = dataclasses.replace(good_chunk(), **change)
    with pytest.raises(ValueError, match=message) as caught:
        replace_chunks(engine, ids.new_ulid(), [wrong], VERSION)
    assert CANARY not in str(caught.value)


def test_only_a_chunker_this_application_has_is_accepted() -> None:
    for version in ("structural-2", "structural-1 ", "Structural-1", "", None):
        with pytest.raises(ValueError, match="chunker version"):
            check_chunks([good_chunk()], version)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="out of order"):
        check_chunks(["not a chunk"], VERSION)  # type: ignore[list-item]


# --- what the database refuses -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"chunk_index": -1},
        {"page_start": 0, "page_end": 0},
        {"page_end": 0},
        {"char_start": -1},
        {"char_end": 0},
        {"char_end": 6},
        {"text": "", "char_end": 0},
        {"token_count": 0},
        {"section_path": "not json"},
        {"section_path": '{"a": 1}'},
        {"chunker_version": "Structural-1"},
        {"chunker_version": "structural"},
        {"chunker_version": "structural--1"},
        {"page_start": 2, "page_end": 2},  # page 2 of this version is not stored
    ],
)
def test_a_chunk_of_the_wrong_shape_is_refused_by_the_database(
    engine: Engine, change: dict[str, Any]
) -> None:
    version = add_document(engine)[1]
    replace_pages(engine, version, [PageText(1, "hello")], "text-1", None)
    document = document_of(engine, version)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(chunks).values(chunk_row(version, document, **change)))
    assert index_rows(engine) == []


def test_a_chunk_must_name_its_versions_own_document(engine: Engine) -> None:
    version = add_document(engine)[1]
    replace_pages(engine, version, [PageText(1, "hello")], "text-1", None)
    other_document = add_document(engine)[0]
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(chunks).values(chunk_row(version, other_document)))


def test_a_chunk_index_is_used_once_per_version(engine: Engine) -> None:
    version = add_document(engine)[1]
    replace_pages(engine, version, [PageText(1, "hello")], "text-1", None)
    document = document_of(engine, version)
    with engine.begin() as connection:
        connection.execute(insert(chunks).values(chunk_row(version, document)))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(chunks).values(chunk_row(version, document)))


# --- the index follows every change --------------------------------------------------------------


def test_new_pages_delete_the_versions_chunks_and_their_index_rows(
    engine: Engine, version_id: str
) -> None:
    store(engine, version_id)
    replace_pages(engine, version_id, course_pages(), "text-1", "utf-8")
    with engine.connect() as connection:
        assert connection.execute(select(chunks)).all() == []
    assert index_rows(engine) == []
    assert_index_in_step(engine)


def test_deleting_a_document_deletes_its_chunks_and_their_index_rows(
    engine: Engine, version_id: str
) -> None:
    store(engine, version_id)
    document = document_of(engine, version_id)
    with engine.begin() as connection:
        connection.execute(
            update(documents).where(documents.c.id == document).values(current_version_id=None)
        )
        connection.execute(delete(documents).where(documents.c.id == document))
    with engine.connect() as connection:
        assert connection.execute(select(chunks)).all() == []
        assert connection.execute(select(pages)).all() == []
    assert index_rows(engine) == []
    assert_index_in_step(engine)


def test_a_hand_edit_of_a_chunk_is_followed_by_the_index(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    with engine.begin() as connection:
        connection.execute(
            update(chunks)
            .where(chunks.c.chunk_index == 0)
            .values(section_path='["Renamed"]', token_count=3)
        )
    assert_index_in_step(engine)
    assert search(engine, "section_path:renamed") == [0]


# --- keyword search ------------------------------------------------------------------------------


def test_a_phrase_query_finds_the_words_together_only(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    # Chunk 2 holds both words, apart and in another order.
    assert search(engine, '"equivalent partitioning"') == [0]
    assert sorted(search(engine, "equivalent partitioning")) == [0, 2]


def test_a_course_code_is_found_as_a_phrase(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    # Chunk 2 has "485 CSE" and "CSE 486", which a phrase query must not match.
    assert search(engine, '"CSE 485"') == [1]
    assert search(engine, '"cse 485"') == [1]
    assert sorted(search(engine, "CSE 485")) == [1, 2]


def test_words_are_stemmed_and_folded(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    assert sorted(search(engine, "partitions")) == [0, 1, 2]  # porter: partition*
    assert search(engine, "tuesday") == [1]
    assert search(engine, "cafe") == [2]  # unicode61 folds the accent
    assert search(engine, CAFE.upper()) == [2]
    assert search(engine, CJK) == [2]


def test_titles_and_section_paths_are_searchable(engine: Engine, version_id: str) -> None:
    store(engine, version_id)
    assert sorted(search(engine, "section_path:grading")) == [1, 2]
    assert sorted(search(engine, "title:syllabus")) == [0, 1, 2]  # the first import's file name
    assert search(engine, "title:tuesdays") == []
    rows = index_rows(engine)
    assert {row[2] for row in rows} == {"Syllabus.pdf"}


# --- the migration -------------------------------------------------------------------------------


def test_the_database_holds_exactly_the_fts_tables_the_comparison_leaves_out(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%fts%'")
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'chunks'"
                )
            )
        }
        definition: str = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'")
        ).scalar_one()
        table_sql: str = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE name = 'chunks'")
        ).scalar_one()
    assert names == FTS_TABLES
    assert triggers == {"chunks_fts_insert", "chunks_fts_delete", "chunks_fts_update"}
    with engine.connect() as connection:
        source_triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master"
                    " WHERE type = 'trigger' AND tbl_name = 'import_sources'"
                )
            )
        }
    assert source_triggers == {
        "chunks_fts_title_after_source_insert",
        "chunks_fts_title_after_source_delete",
        "chunks_fts_title_after_source_update",
    }
    assert "tokenize = 'porter unicode61'" in definition
    assert "AUTOINCREMENT" in table_sql


def test_the_chunks_table_can_be_undone_with_chunks_in_it(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        version = add_document(engine)[1]
        replace_pages(engine, version, course_pages(), "text-1", "utf-8")
        store(engine, version)
    finally:
        engine.dispose()

    command.downgrade(alembic_config(db_file), "0004")
    engine = create_vault_engine(db_file)
    try:
        names = set(inspect(engine).get_table_names())
        assert "chunks" not in names
        assert not names & FTS_TABLES
        assert "pages" in names
    finally:
        engine.dispose()
    upgrade_to_latest(db_file)


# --- the indexed title is the name the library shows ---------------------------------------------


def assert_titles_are_the_library_names(engine: Engine) -> None:
    """Every document's index rows carry the name the document list and the detail panel show
    (``vault.db.documents``, the one definition of a document's name), or nothing without one."""
    listed = list_documents(engine, ListQuery(limit=100)).items
    assert listed
    with engine.connect() as connection:
        titles = {
            (row.document_id, row.title)
            for row in connection.execute(
                text(
                    "SELECT chunks.document_id, chunks_fts.title FROM chunks"
                    " JOIN chunks_fts ON chunks_fts.rowid = chunks.id"
                )
            )
        }
    expected = set()
    for summary in listed:
        detail = get_document(engine, summary.id)
        assert detail is not None
        assert detail.summary.name == summary.name
        expected.add((summary.id, summary.name or ""))
    with_chunks = {document for document, _ in titles}
    assert titles == {pair for pair in expected if pair[0] in with_chunks}
    assert_index_in_step(engine)


def chunked_document(engine: Engine) -> str:
    document, version = add_document(engine)
    replace_pages(engine, version, course_pages(), "text-1", "utf-8")
    store(engine, version)
    return document


def first_source(engine: Engine, document: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                select(import_sources.c.id)
                .where(import_sources.c.document_id == document)
                .order_by(import_sources.c.imported_at, import_sources.c.id)
                .limit(1)
            ).scalar_one()
        )


def add_source(engine: Engine, document: str, **changes: Any) -> str:
    row = source_row(document, **changes)
    with engine.begin() as connection:
        connection.execute(insert(import_sources).values(row))
    return str(row["id"])


def test_a_later_import_with_an_earlier_time_renames_the_index_rows(engine: Engine) -> None:
    document = chunked_document(engine)
    add_source(engine, document, original_filename="Earlier.pdf", imported_at=NOW - timedelta(1))
    assert_titles_are_the_library_names(engine)
    assert sorted(search(engine, "title:earlier")) == [0, 1, 2]
    assert search(engine, "title:syllabus") == []


def test_a_later_import_with_the_same_time_and_a_lower_id_renames_the_index_rows(
    engine: Engine,
) -> None:
    document = chunked_document(engine)
    lowest = ids.new_ulid(now_ms=1)  # sorts before every id made now
    add_source(engine, document, id=lowest, original_filename="Tie.pdf", imported_at=NOW)
    assert_titles_are_the_library_names(engine)
    assert sorted(search(engine, "title:tie")) == [0, 1, 2]


def test_a_later_import_that_sorts_last_changes_nothing(engine: Engine) -> None:
    document = chunked_document(engine)
    before = index_rows(engine)
    add_source(engine, document, original_filename="Copy.pdf", imported_at=NOW + timedelta(1))
    assert index_rows(engine) == before
    assert_titles_are_the_library_names(engine)


def test_removing_or_editing_the_first_import_renames_the_index_rows(engine: Engine) -> None:
    document = chunked_document(engine)
    add_source(engine, document, original_filename="Second.pdf", imported_at=NOW + timedelta(1))
    with engine.begin() as connection:
        connection.execute(
            update(import_sources)
            .where(import_sources.c.id == first_source(engine, document))
            .values(original_filename="Edited.pdf")
        )
    assert_titles_are_the_library_names(engine)
    assert sorted(search(engine, "title:edited")) == [0, 1, 2]

    with engine.begin() as connection:
        connection.execute(
            delete(import_sources).where(import_sources.c.id == first_source(engine, document))
        )
    assert_titles_are_the_library_names(engine)
    assert sorted(search(engine, "title:second")) == [0, 1, 2]

    with engine.begin() as connection:
        connection.execute(delete(import_sources).where(import_sources.c.document_id == document))
    assert_titles_are_the_library_names(engine)
    assert {row[2] for row in index_rows(engine)} == {""}


def test_moving_an_import_to_another_document_renames_both(engine: Engine) -> None:
    one = chunked_document(engine)
    two = chunked_document(engine)
    moved = add_source(engine, one, original_filename="Moved.pdf", imported_at=NOW - timedelta(1))
    assert_titles_are_the_library_names(engine)
    with engine.begin() as connection:
        connection.execute(
            update(import_sources).where(import_sources.c.id == moved).values(document_id=two)
        )
    assert_titles_are_the_library_names(engine)


def test_generated_changes_to_imports_keep_every_title_the_library_name(engine: Engine) -> None:
    rng = random.Random(57)  # noqa: S311 - seeded test data, not a secret
    documents_made = [chunked_document(engine) for _ in range(3)]
    sources: list[str] = []
    for step in range(60):
        action = rng.choice(["insert", "insert", "delete", "rename", "retime", "move"])
        if action == "insert" or not sources:
            sources.append(
                add_source(
                    engine,
                    rng.choice(documents_made),
                    id=ids.new_ulid(now_ms=rng.choice([1, 2, 10**12])),
                    original_filename=f"name{step}.pdf",
                    imported_at=NOW + timedelta(seconds=rng.choice([-5, 0, 0, 5])),
                )
            )
            continue
        chosen = rng.choice(sources)
        all_changes: dict[str, dict[str, Any]] = {
            "rename": {"original_filename": f"renamed{step}.pdf"},
            "retime": {"imported_at": NOW + timedelta(seconds=rng.randint(-9, 9))},
            "move": {"document_id": rng.choice(documents_made)},
        }
        changes = all_changes.get(action, {})
        with engine.begin() as connection:
            where = import_sources.c.id == chosen
            if action == "delete":
                connection.execute(delete(import_sources).where(where))
                sources.remove(chosen)
            else:
                connection.execute(update(import_sources).where(where).values(**changes))
        assert_titles_are_the_library_names(engine)
