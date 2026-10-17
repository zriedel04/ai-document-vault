"""The pages table and its repository: what is stored, what is refused, and what comes back."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, delete, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from tests.unit.test_documents_schema import add_document
from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import alembic_config, upgrade_to_latest
from vault.db.pages import (
    PageDataError,
    PageHeading,
    PageText,
    StoredPages,
    VersionNotFoundError,
    read_pages,
    replace_pages,
)
from vault.db.schema import documents, pages

# Synthetic text with letters outside ASCII, built from code points.
CAFE = "café — 履歴書"
CANARY = "canary-page-text-xyzzy"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def version_id(engine: Engine) -> str:
    return add_document(engine)[1]


def two_pages() -> list[PageText]:
    first = f"# Notes\n{CAFE}\n## Plan\nstep one\n"
    plan = first.index("## Plan")
    headings = (PageHeading(1, "Notes", 0, 7), PageHeading(2, "Plan", plan, plan + 7))
    return [PageText(1, first, headings), PageText(2, "", ())]


def page_row(version_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "version_id": version_id,
        "page_no": 1,
        "text": "hello",
        "char_count": 5,
        "headings": "[]",
        "extractor": "text-1",
        "encoding": "utf-8",
    } | changes


# --- storing and reading -------------------------------------------------------------------------


def test_pages_come_back_as_they_were_stored(engine: Engine, version_id: str) -> None:
    replace_pages(engine, version_id, two_pages(), "text-1", "utf-8")

    stored = read_pages(engine, version_id)

    assert stored == StoredPages("text-1", "utf-8", tuple(two_pages()))
    first = stored.pages[0]
    assert [first.text[h.start : h.end] for h in first.headings] == ["# Notes", "## Plan"]


def test_the_char_count_is_the_texts_length_in_characters(engine: Engine, version_id: str) -> None:
    replace_pages(engine, version_id, [PageText(1, CAFE)], "text-1", None)

    with engine.connect() as connection:
        assert connection.execute(select(pages.c.char_count)).scalar_one() == len(CAFE) == 10


def test_a_second_extraction_replaces_every_page_of_the_first(
    engine: Engine, version_id: str
) -> None:
    replace_pages(engine, version_id, [PageText(n, f"page {n}") for n in (1, 2, 3)], "pdf-1", None)
    replace_pages(engine, version_id, [PageText(1, "only page")], "pdf-2", None)

    stored = read_pages(engine, version_id)
    assert stored == StoredPages("pdf-2", None, (PageText(1, "only page"),))


def test_another_versions_pages_are_left_alone(engine: Engine, version_id: str) -> None:
    other = add_document(engine)[1]
    replace_pages(engine, other, [PageText(1, "other")], "text-1", "utf-8")

    replace_pages(engine, version_id, [PageText(1, "mine")], "text-1", "utf-8")

    stored = read_pages(engine, other)
    assert stored is not None
    assert stored.pages == (PageText(1, "other"),)


def test_a_version_not_extracted_yet_has_no_pages(engine: Engine, version_id: str) -> None:
    assert read_pages(engine, version_id) is None
    assert read_pages(engine, ids.new_ulid()) is None
    assert read_pages(engine, "../vault.db") is None


@pytest.mark.parametrize("bad_id", ["not-a-ulid", "", "../vault.db"])
def test_an_id_that_names_no_version_is_refused_and_nothing_is_written(
    engine: Engine, version_id: str, bad_id: str
) -> None:
    for missing in (bad_id, ids.new_ulid()):
        with pytest.raises(VersionNotFoundError):
            replace_pages(engine, missing, [PageText(1, "x")], "text-1", None)
    with engine.connect() as connection:
        assert connection.execute(select(pages)).all() == []


def test_refused_pages_leave_the_stored_ones_as_they_were(engine: Engine, version_id: str) -> None:
    replace_pages(engine, version_id, two_pages(), "text-1", "utf-8")

    with pytest.raises(ValueError, match="Page 2"):
        replace_pages(engine, version_id, [PageText(1, "a"), PageText(3, "b")], "text-1", None)

    assert read_pages(engine, version_id) == StoredPages("text-1", "utf-8", tuple(two_pages()))


def test_a_failure_inside_the_transaction_writes_nothing(engine: Engine, version_id: str) -> None:
    replace_pages(engine, version_id, [PageText(1, "kept")], "text-1", "utf-8")
    with engine.begin() as connection:
        connection.execute(update(documents).values(current_version_id=None))
        connection.execute(delete(documents))  # the versions and their pages go with it
    with pytest.raises(VersionNotFoundError):
        replace_pages(engine, version_id, [PageText(1, "new")], "text-1", "utf-8")
    with engine.connect() as connection:
        assert connection.execute(select(pages)).all() == []


def test_deleting_a_document_deletes_its_pages(engine: Engine, version_id: str) -> None:
    replace_pages(engine, version_id, two_pages(), "text-1", "utf-8")

    with engine.begin() as connection:
        connection.execute(update(documents).values(current_version_id=None))
        connection.execute(delete(documents))

    with engine.connect() as connection:
        assert connection.execute(select(pages)).all() == []


# --- what the repository refuses -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages_in", "message"),
    [
        ([], "at least one page"),
        ([PageText(2, "a")], "Page 1"),
        ([PageText(1, "a"), PageText(1, "b")], "Page 2"),
        ([PageText(1, "a"), PageText(3, "b")], "Page 2"),
        ([PageText(True, "a")], "Page 1"),
        (["page one"], "Page 1"),
        ([PageText(1, "a\x00b")], "NUL"),
        ([PageText(1, "lone \ud800 surrogate")], "NUL"),
        ([PageText(1, b"bytes")], "NUL"),  # type: ignore[arg-type]
        ([PageText(1, "abc", [PageHeading(1, "a", 0, 1)])], "tuple"),  # type: ignore[arg-type]
        ([PageText(1, "abc", ("heading",))], "not a heading"),  # type: ignore[arg-type]
        ([PageText(1, "abc", (PageHeading(0, "a", 0, 1),))], "level"),
        ([PageText(1, "abc", (PageHeading(7, "a", 0, 1),))], "level"),
        ([PageText(1, "abc", (PageHeading(True, "a", 0, 1),))], "level"),
        ([PageText(1, "abc", (PageHeading(1, "a\x00", 0, 1),))], "title"),
        ([PageText(1, "abc", (PageHeading(1, None, 0, 1),))], "title"),  # type: ignore[arg-type]
        ([PageText(1, "abc", (PageHeading(1, "a", 0.0, 1),))], "whole numbers"),  # type: ignore[arg-type]
        ([PageText(1, "abc", (PageHeading(1, "a", False, 1),))], "whole numbers"),
        ([PageText(1, "abc", (PageHeading(1, "a", -1, 1),))], "outside"),
        ([PageText(1, "abc", (PageHeading(1, "a", 0, 4),))], "outside"),
        ([PageText(1, "abc", (PageHeading(1, "a", 1, 1),))], "outside"),
        ([PageText(1, "abc", (PageHeading(1, "a", 2, 1),))], "outside"),
        (
            [PageText(1, "abcdef", (PageHeading(1, "a", 0, 3), PageHeading(2, "b", 2, 4)))],
            "overlaps",
        ),
        (
            [PageText(1, "abcdef", (PageHeading(1, "a", 3, 4), PageHeading(2, "b", 0, 1)))],
            "overlaps",
        ),
    ],
)
def test_pages_that_break_the_rules_are_refused(
    engine: Engine, version_id: str, pages_in: list[PageText], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace_pages(engine, version_id, pages_in, "text-1", None)
    assert read_pages(engine, version_id) is None


def test_headings_may_touch_and_an_empty_title_is_kept(engine: Engine, version_id: str) -> None:
    page = PageText(1, "#\n# B", (PageHeading(1, "", 0, 1), PageHeading(1, "B", 1, 5)))

    replace_pages(engine, version_id, [page], "text-1", "utf-8")

    stored = read_pages(engine, version_id)
    assert stored is not None
    assert stored.pages == (page,)


@pytest.mark.parametrize("extractor", ["text-1", "pdf-12", "pdf-plumber-3", "docx2-1", "a1-999999"])
def test_extractor_names_that_are_accepted(engine: Engine, version_id: str, extractor: str) -> None:
    replace_pages(engine, version_id, [PageText(1, "x")], extractor, None)
    stored = read_pages(engine, version_id)
    assert stored is not None
    assert stored.extractor == extractor


@pytest.mark.parametrize(
    "extractor",
    [
        "text",
        "Text-1",
        "text-0",
        "text-01",
        "text-1\n",
        "text-1\r",
        "text-1 ",
        " text-1",
        "text--1",
        "-1",
        "1-1",
        "text-1-",
        "text_one-1",
        "",
        "a" * 39 + "-1",
        "text-1234567",
        None,
        1,
    ],
)
def test_extractor_names_that_are_refused(
    engine: Engine, version_id: str, extractor: object
) -> None:
    with pytest.raises(ValueError, match="extractor"):
        replace_pages(engine, version_id, [PageText(1, "x")], extractor, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "encoding", [None, "utf-8", "cp1252", "cp1251", "utf-16-le", "utf-32-be", "gbk", "cp932"]
)
def test_encodings_that_are_accepted(engine: Engine, version_id: str, encoding: str | None) -> None:
    replace_pages(engine, version_id, [PageText(1, "x")], "text-1", encoding)
    stored = read_pages(engine, version_id)
    assert stored is not None
    assert stored.encoding == encoding


@pytest.mark.parametrize(
    "encoding",
    ["UTF-8", "utf8", "latin-1", "windows-1252", "no-such-codec", "utf-8\n", "utf-8 ", "", 8],
)
def test_encodings_that_are_not_a_canonical_codec_name_are_refused(
    engine: Engine, version_id: str, encoding: object
) -> None:
    with pytest.raises(ValueError, match="encoding"):
        replace_pages(engine, version_id, [PageText(1, "x")], "text-1", encoding)  # type: ignore[arg-type]


def test_no_error_quotes_the_text(engine: Engine, version_id: str) -> None:
    bad = [
        [PageText(1, f"{CANARY}\x00")],
        [PageText(1, CANARY, (PageHeading(1, CANARY, 0, len(CANARY) + 1),))],
        [PageText(1, CANARY, (PageHeading(9, CANARY, 0, 1),))],
    ]
    for pages_in in bad:
        with pytest.raises(ValueError, match=r"[Pp]age 1") as caught:
            replace_pages(engine, version_id, pages_in, "text-1", None)
        assert CANARY not in str(caught.value)


# --- what the database refuses -------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"page_no": 0},
        {"char_count": 4},
        {"text": "a\x00b", "char_count": 3},
        {"headings": "not json"},
        {"headings": "{}"},
        {"headings": '"text"'},
        {"extractor": "Text-1"},
        {"extractor": "text"},
        {"extractor": "text-1 "},
        {"extractor": "text--1"},
        {"extractor": "text-0"},
        {"encoding": "UTF-8"},
        {"encoding": ""},
        {"encoding": "utf 8"},
    ],
)
def test_a_page_of_the_wrong_shape_is_refused_by_the_database(
    engine: Engine, version_id: str, change: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(pages).values(page_row(version_id, **change)))


def test_a_page_must_belong_to_a_version(engine: Engine) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(pages).values(page_row(ids.new_ulid())))


def test_a_page_number_is_used_once_per_version(engine: Engine, version_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(insert(pages).values(page_row(version_id)))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(pages).values(page_row(version_id)))


def test_every_extractor_and_encoding_the_application_accepts_fits_the_table(
    engine: Engine, version_id: str
) -> None:
    for extractor in ("text-1", "pdf-12", "pdf-plumber-3", "docx2-1", "a1-999999", "a" * 38 + "-1"):
        for encoding in (None, "utf-8", "utf-16-le", "cp1252", "gbk", "shift_jis", "iso8859-1"):
            replace_pages(engine, version_id, [PageText(1, "x")], extractor, encoding)


# --- what reading refuses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"page_no": 2}], "Page 1"),
        ([{"page_no": 1}, {"page_no": 3}], "Page 2"),
        ([{"page_no": 1}, {"page_no": 2, "extractor": "text-2"}], "different extractors"),
        ([{"page_no": 1}, {"page_no": 2, "encoding": None}], "different extractors"),
        ([{"headings": "[[1, 1, 0, 1]]"}], "title"),
        ([{"headings": '[[1, "t", 0, 9]]'}], "outside"),
        ([{"headings": '[[1, "t", 0.0, 1]]'}], "whole numbers"),
        ([{"headings": '[[1, "t", 0]]'}], "level, title, start, end"),
        ([{"headings": '[{"level": 1}]'}], "level, title, start, end"),
        ([{"headings": '[[true, "t", 0, 1]]'}], "level"),
        ([{"extractor": "text-1-2"}], "extractor"),
        ([{"encoding": "utf8"}], "encoding"),
    ],
)
def test_stored_pages_that_break_the_rules_are_refused_when_read(
    engine: Engine, version_id: str, rows: list[dict[str, Any]], message: str
) -> None:
    with engine.begin() as connection:
        for row in rows:
            connection.execute(insert(pages).values(page_row(version_id, **row)))

    with pytest.raises(PageDataError, match=message) as caught:
        read_pages(engine, version_id)
    assert "hello" not in str(caught.value)


def test_stored_headings_hold_the_titles_as_written(engine: Engine, version_id: str) -> None:
    title = f"{CAFE} « quoted »"
    text = f"# {title}\n"
    replace_pages(
        engine,
        version_id,
        [PageText(1, text, (PageHeading(1, title, 0, len(text) - 1),))],
        "text-1",
        "utf-8",
    )

    with engine.connect() as connection:
        stored: str = connection.execute(select(pages.c.headings)).scalar_one()
    assert json.loads(stored) == [[1, title, 0, len(text) - 1]]


# --- the migration -------------------------------------------------------------------------------


def test_the_pages_table_can_be_undone_with_pages_in_it(tmp_path: Path) -> None:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        replace_pages(engine, add_document(engine)[1], two_pages(), "text-1", "utf-8")
    finally:
        engine.dispose()

    command.downgrade(alembic_config(db_file), "0003")
    engine = create_vault_engine(db_file)
    try:
        assert "pages" not in inspect(engine).get_table_names()
        assert "documents" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    upgrade_to_latest(db_file)


def test_headings_that_are_not_json_are_refused_when_read(engine: Engine, version_id: str) -> None:
    # The database refuses this row; a hand edit can switch its checks off first.
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(insert(pages).values(page_row(version_id, headings="not json")))
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(PageDataError, match="level, title, start, end"):
        read_pages(engine, version_id)
