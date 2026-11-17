"""Keyword search: the query grammar, scope, ranking, snippets and the endpoint (synthetic text)."""

from __future__ import annotations

import logging
import random
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, select, text, update

from tests.unit.test_documents_api import Library
from vault.api.search import Extension, parse_instant
from vault.core.config import Settings
from vault.db import ids
from vault.db.pages import PageHeading, PageText, replace_pages
from vault.db.schema import chunks, document_versions, documents
from vault.ingest.chunking.structural_v1 import CHUNKER_VERSION, chunk_pages
from vault.ingest.validate import MIME_TYPES
from vault.main import create_app
from vault.retrieval.keyword import (
    MAX_QUERY_CHARS,
    MAX_TERMS,
    QueryError,
    SearchQuery,
    build_match,
    search,
)

from ..conftest import LOCAL

URL = "/api/search"
CANARY = "Patient-Diagnosis-Canary"


@pytest.fixture
def library(app: FastAPI, tmp_path: Path) -> Library:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Library(app, folder)


def current_version(engine: Engine, document_id: str) -> str:
    with engine.connect() as connection:
        version: str = connection.execute(
            select(documents.c.current_version_id).where(documents.c.id == document_id)
        ).scalar_one()
    return version


def index(engine: Engine, version_id: str, *pages: PageText) -> None:
    """Store the pages of a version and its chunks, as the extraction and chunking stages will."""
    from vault.db.chunks import replace_chunks

    replace_pages(engine, version_id, pages, "text-1", "utf-8")
    replace_chunks(engine, version_id, chunk_pages(pages), CHUNKER_VERSION)


def add(library: Library, name: str, *page_texts: str) -> str:
    document_id = library.add(name, "\n".join(page_texts))
    version = current_version(library.engine, document_id)
    pages = [PageText(number, body) for number, body in enumerate(page_texts, start=1)]
    index(library.engine, version, *pages)
    return document_id


def find(client: TestClient, query: str, **body: Any) -> dict[str, Any]:
    response = client.post(URL, json={"query": query, **body})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def found(client: TestClient, query: str, **body: Any) -> list[str]:
    return [item["document_id"] for item in find(client, query, **body)["items"]]


def marked(item: dict[str, Any]) -> list[str]:
    return [part["text"] for part in item["snippet"] if part["match"]]


# --- the query grammar --------------------------------------------------------------------------


def test_words_are_quoted_terms_that_must_all_match() -> None:
    assert build_match("boundary values") == '"boundary" AND "values"'


def test_quotes_make_a_phrase_and_an_unclosed_quote_runs_to_the_end() -> None:
    assert build_match('"equivalent partitioning" grade') == '"equivalent partitioning" AND "grade"'
    assert build_match('grade "equivalent partitioning') == '"grade" AND "equivalent partitioning"'


def test_a_trailing_star_is_a_prefix() -> None:
    assert build_match("partit*") == '"partit"*'
    assert build_match("*") is None


def test_codes_match_with_and_without_the_space() -> None:
    assert build_match("CSE485") == '("CSE485" OR "CSE 485")'
    assert build_match("CSE 485") == '(("CSE" AND "485") OR "CSE485")'
    assert build_match("(CSE485),") == '("CSE485" OR "CSE 485")'
    assert build_match("CSE 485 exam") == '(("CSE" AND "485") OR "CSE485") AND "exam"'
    # A mark between the letters and the digits: the code joined and apart, and the word as typed
    # (a mark SQLite's tables do not know is part of the token, and the word must find itself).
    assert build_match("CSE-485") == '("CSE-485" OR "CSE485" OR "CSE 485")'
    assert build_match("(cse_485),") == '("cse_485" OR "cse485" OR "cse 485")'
    assert build_match("CSE-485-01") == '"CSE-485-01"'
    # A phrase or a prefix is taken as typed.
    assert build_match('CSE "485"') == '"CSE" AND "485"'
    assert build_match("CSE 48*") == '"CSE" AND "48"*'


def test_a_query_without_a_letter_or_digit_has_no_term() -> None:
    for query in ("", "   ", "- & !", '""', '" "', "☃ ✓", "***"):
        assert build_match(query) is None, query


def test_query_syntax_is_text_not_operators() -> None:
    query = 'a OR b NOT c NEAR(d e) title:f ^g {text}: h + "i""'
    match = build_match(query)
    assert (
        match
        == " AND ".join(
            f'"{word}"' for word in ("a", "OR", "b", "NOT", "c", "NEAR(d", "e)", "title:f", "^g")
        )
        + ' AND "{text}:" AND "h" AND "i"'
    )


def test_long_queries_and_many_terms_are_refused_without_quoting_them() -> None:
    with pytest.raises(QueryError) as error:
        build_match(CANARY + "x" * MAX_QUERY_CHARS)
    assert CANARY not in str(error.value)
    with pytest.raises(QueryError) as error:
        build_match(" ".join([CANARY] * (MAX_TERMS + 1)))
    assert CANARY not in str(error.value)
    assert build_match(" ".join(["word"] * MAX_TERMS)) is not None
    assert build_match("x" * MAX_QUERY_CHARS) is not None


def test_text_sqlite_cannot_store_is_refused() -> None:
    for query in ("a\x00b", "a\ud800b"):
        with pytest.raises(QueryError):
            build_match(query)


def test_no_generated_query_is_an_fts5_error(engine_with_index: Engine) -> None:
    """Whatever the owner types, the expression is valid FTS5: random text from the characters
    FTS5 gives a meaning, letters, digits and other scripts never makes SQLite raise."""
    alphabet = list('"*()^:{}+-., \tORANDNOTNEAR') + list("abcXYZ0129") + ["é", "履", "☃", "_"]
    generator = random.Random(485)  # noqa: S311  (test data, not a secret)
    with engine_with_index.connect() as connection:
        for _ in range(3000):
            query = "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 30)))
            try:
                match = build_match(query)
            except QueryError:
                continue
            if match is not None:
                connection.execute(
                    text("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH :m"), {"m": match}
                ).scalar_one()


@pytest.fixture
def engine_with_index(app: FastAPI, library: Library) -> Engine:
    add(library, "syllabus.md", "CSE 485 covers equivalent partitioning.")
    engine: Engine = app.state.engine
    return engine


# --- search ---------------------------------------------------------------------------------------


def test_a_word_finds_the_document_with_a_marked_snippet(
    client: TestClient,
    library: Library,
) -> None:
    syllabus = add(
        library,
        "syllabus.md",
        "Course overview and policies.",
        "Boundary values are tested at the edges of each partition.",
    )
    add(library, "receipt.txt", "Coffee 3.50, total 7.00")
    result = find(client, "boundary")
    assert result["total"] == 1
    [item] = result["items"]
    assert item["document_id"] == syllabus
    assert item["name"] == "syllabus.md"
    assert item["extension"] == "md"
    assert item["page"] == 2
    assert item["matches"] == 1
    assert marked(item) == ["Boundary"]
    assert "".join(part["text"] for part in item["snippet"]).startswith("Boundary values")


def test_stems_case_and_accents_are_folded(client: TestClient, library: Library) -> None:
    notes = add(library, "notes.txt", "Equivalent Partitioning at the café on Tuesday.")
    assert found(client, "partition") == [notes]
    assert found(client, "PARTITIONED") == [notes]
    assert found(client, "cafe") == [notes]
    assert found(client, "partit*") == [notes]


def test_a_phrase_needs_its_words_together_and_in_order(
    client: TestClient,
    library: Library,
) -> None:
    together = add(library, "a.txt", "We used equivalent partitioning here.")
    add(library, "b.txt", "Partitioning is not equivalent to guessing.")
    apart = found(client, "guessing")
    assert sorted(found(client, "equivalent partitioning")) == sorted([together, *apart])
    assert found(client, '"equivalent partitioning"') == [together]


def test_course_codes_match_with_or_without_a_space(
    client: TestClient,
    library: Library,
) -> None:
    spaced = add(library, "spaced.txt", "Welcome to CSE 485 this term.")
    joined = add(library, "joined.txt", "Notes for CSE485, week two.")
    apart = add(library, "apart.txt", "CSE 486 and room 485 are elsewhere.")
    add(library, "neither.txt", "Room 486 only.")
    assert sorted(found(client, "CSE485")) == sorted([spaced, joined])
    # Typed apart, both words must match anywhere (as for any two words), or the code joined.
    assert sorted(found(client, "cse 485")) == sorted([spaced, joined, apart])
    # A phrase is taken as typed: the words together, which a joined code is not.
    assert found(client, '"CSE 485"') == [spaced]


# Every way a document or a query writes a code: the tokenizer splits on anything that is not a
# letter or digit, so all but the joined form index as the two words ``cse`` and ``485``.
CODE_FORMS = ("CSE 485", "CSE485", "CSE-485", "CSE_485", "CSE/485", "CSE.485", "CSE\u2013485")


def test_every_written_form_of_a_code_finds_every_other(
    client: TestClient,
    library: Library,
) -> None:
    documents = {
        form: add(library, f"form-{number}.txt", f"Notes for {form}, week two.")
        for number, form in enumerate(CODE_FORMS)
    }
    add(library, "other.txt", "CSE 486 and CSE486 and CSE-486 meet elsewhere.")
    expected = sorted(documents.values())
    for form in CODE_FORMS:
        for query in (form, form.lower(), f"({form}),"):
            assert sorted(found(client, query)) == expected, query


def test_every_term_must_match(client: TestClient, library: Library) -> None:
    both = add(library, "both.txt", "Grading rubric for the capstone.")
    add(library, "one.txt", "Grading only.")
    assert found(client, "grading capstone") == [both]
    assert found(client, "grading nowhere") == []


def test_the_file_name_and_the_section_are_searched_too(
    client: TestClient,
    library: Library,
) -> None:
    document_id = library.add("Lease-Agreement.md", "# Deposit\nIt is paid in March.\n")
    version = current_version(library.engine, document_id)
    body = "# Deposit\nIt is paid in March.\n"
    index(library.engine, version, PageText(1, body, (PageHeading(1, "Deposit", 0, 9),)))
    assert found(client, "lease") == [document_id]
    [item] = find(client, "deposit march")["items"]
    assert item["section_path"] == ["Deposit"]
    assert marked(item) == ["Deposit", "March"]


def segments_marked(segments: list[dict[str, Any]]) -> list[str]:
    return [part["text"] for part in segments if part["match"]]


def test_a_match_in_the_name_or_a_section_is_marked_there(
    client: TestClient,
    library: Library,
) -> None:
    """The name and the section path are searched, so a result found by them alone shows where:
    the text snippet has no mark then, and a match by name alone shows the first chunk."""
    title = 'Say "hi" \\ to Lease terms'  # a quote and a backslash, escaped in the stored JSON
    document_id = library.add("Lease-Agreement.md", "body")
    version = current_version(library.engine, document_id)
    first = "# Intro\nNothing to see on page one.\n"
    second = f"# {title}\nThe deposit is due.\n"
    index(
        library.engine,
        version,
        PageText(1, first, (PageHeading(1, "Intro", 0, 7),)),
        PageText(2, second, (PageHeading(1, title, 0, len(title) + 2),)),
    )
    [by_name] = find(client, "agreement")["items"]
    assert segments_marked(by_name["name_highlight"]) == ["Agreement"]
    assert "".join(part["text"] for part in by_name["name_highlight"]) == "Lease-Agreement.md"
    assert marked(by_name) == []
    assert by_name["page"] == 1  # every chunk scores the same: the first one is shown
    [by_section] = find(client, "terms")["items"]
    assert by_section["page"] == 2
    assert by_section["section_path"] == [title]
    [section] = by_section["section_highlight"]
    assert segments_marked(section) == ["terms"]
    assert "".join(part["text"] for part in section) == title
    assert segments_marked(by_section["name_highlight"]) == []
    [by_text] = find(client, "deposit")["items"]
    assert marked(by_text) == ["deposit"]
    assert by_text["name_highlight"] == [{"text": "Lease-Agreement.md", "match": False}]
    assert by_text["section_highlight"] == [[{"text": title, "match": False}]]


def test_a_marked_section_list_that_reads_back_differently_is_not_used() -> None:
    from vault.retrieval.keyword import _marked_titles

    o, c = "<o>", "<c>"
    assert _marked_titles('["a <o>b<c>"]', ("a b",), o, c) is not None
    assert _marked_titles('["a <o>b<c>", "x"]', ("a b",), o, c) is None
    assert _marked_titles('["a <o>c<c>"]', ("a b",), o, c) is None
    assert _marked_titles("not json", ("a b",), o, c) is None
    assert _marked_titles("[1]", ("1",), o, c) is None


def test_one_result_per_document_with_its_best_chunk_and_match_count(
    client: TestClient,
    library: Library,
) -> None:
    many = add(
        library,
        "many.txt",
        "Budget note.",
        "Budget budget budget, the budget for the budget.",
        "A budget again.",
    )
    once = add(library, "once.txt", "One budget line in a long text " + "filler " * 40)
    result = find(client, "budget")
    assert result["total"] == 2
    assert [item["document_id"] for item in result["items"]] == [many, once]
    first = result["items"][0]
    assert first["matches"] == 3
    assert first["page"] == 2


def test_a_match_by_name_alone_points_at_the_first_chunk(
    client: TestClient, library: Library
) -> None:
    # BM25 normalises by the length of the whole row, so a shorter later chunk would score better
    # on the name, which every chunk of the document carries alike.
    first_page = "An opening page with a good deal of text. " * 12
    document_id = add(library, "Quarterly-Report.txt", first_page, "Short.", "Tiny.")
    item = find(client, "quarterly")["items"][0]
    assert item["document_id"] == document_id
    assert (item["page"], item["char_start"], item["matches"]) == (1, 0, 3)
    # When the text matches too, the passage that holds the words is chosen, not the first one.
    item = find(client, "quarterly short")["items"][0]
    assert (item["page"], marked(item)) == (2, ["Short"])


def test_documents_are_ranked_by_their_best_match_over_text_name_and_section(
    client: TestClient, library: Library
) -> None:
    named = add(library, "budget.txt", "Nothing about money here. " * 10)
    plain = add(library, "notes.txt", "A budget line in a long text " + "filler " * 40)
    assert found(client, "budget") == [named, plain]


def test_paging_is_stable_and_counts_documents(client: TestClient, library: Library) -> None:
    for number in range(5):
        add(library, f"memo-{number}.txt", f"Quarterly memo number {number} about the move.")
    everything = find(client, "memo", limit=50)
    assert everything["total"] == 5
    order = [item["document_id"] for item in everything["items"]]
    pages = [find(client, "memo", limit=2, offset=offset) for offset in (0, 2, 4, 6)]
    assert [item["document_id"] for p in pages for item in p["items"]] == order
    assert all(p["total"] == 5 for p in pages)
    assert pages[-1]["items"] == []
    assert found(client, "memo", limit=50) == order


def test_documents_in_the_trash_are_not_found(client: TestClient, library: Library) -> None:
    document_id = add(library, "plan.txt", "The relocation plan.")
    library.trash(document_id)
    assert find(client, "relocation") == {"items": [], "total": 0, "limit": 20, "offset": 0}
    assert client.post(f"/api/documents/{document_id}/restore").status_code == 200
    assert found(client, "relocation") == [document_id]


def test_only_the_current_version_is_searched(client: TestClient, library: Library) -> None:
    document_id = add(library, "draft.txt", "The first draft says apples.")
    engine = library.engine
    old = current_version(engine, document_id)
    with engine.begin() as connection:
        row = connection.execute(select(document_versions).where(document_versions.c.id == old))
        values = dict(row.one()._mapping)
        new = ids.new_ulid()
        connection.execute(insert(document_versions).values(values | {"id": new, "version_no": 2}))
        connection.execute(
            update(documents).where(documents.c.id == document_id).values(current_version_id=new)
        )
    index(engine, new, PageText(1, "The second draft says pears."))
    assert found(client, "pears") == [document_id]
    assert found(client, "apples") == []


def test_types_filter_by_extension(client: TestClient, library: Library) -> None:
    text_file = add(library, "a.txt", "Invoice for March.")
    markdown = add(library, "b.md", "Invoice for April.")
    assert found(client, "invoice", types=["md"]) == [markdown]
    assert found(client, "invoice", types=["txt", "pdf"]) == [text_file]
    assert sorted(found(client, "invoice", types=[])) == sorted([text_file, markdown])
    assert client.post(URL, json={"query": "invoice", "types": ["exe"]}).status_code == 422


def test_the_extension_list_is_the_validators() -> None:
    assert set(typing.get_args(Extension)) == {extension for extension, _kind in MIME_TYPES}


def test_document_text_is_returned_as_text_and_cannot_fake_a_highlight(
    client: TestClient,
    library: Library,
) -> None:
    fake = "" + "0" * 32 + ""
    add(library, "page.txt", f"<b>bold</b> {fake}beware{fake} of the beware marker")
    [item] = find(client, "marker")["items"]
    assert marked(item) == ["marker"]
    joined = "".join(part["text"] for part in item["snippet"])
    assert "<b>bold</b>" in joined
    assert fake in joined


def test_a_query_with_nothing_to_match_finds_nothing(client: TestClient, library: Library) -> None:
    add(library, "a.txt", "Something - & else.")
    assert find(client, " - & ")["items"] == []


def test_refusals_are_fixed_text_and_nothing_is_logged(
    client: TestClient,
    library: Library,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add(library, f"{CANARY}.txt", f"{CANARY} text")
    caplog.set_level(logging.DEBUG)
    assert found(client, CANARY) != []
    too_long = client.post(URL, json={"query": CANARY * 40})
    assert too_long.status_code == 422
    assert CANARY not in too_long.text
    too_many = client.post(URL, json={"query": " ".join([CANARY] * (MAX_TERMS + 1))})
    assert too_many.status_code == 422
    assert CANARY not in too_many.text
    assert CANARY not in caplog.text
    assert CANARY.lower() not in caplog.text.lower()


def test_limits_are_checked(client: TestClient) -> None:
    for body in ({"limit": 0}, {"limit": 51}, {"offset": -1}, {"offset": 1_000_000_001}):
        assert client.post(URL, json={"query": "a", **body}).status_code == 422
    with pytest.raises(ValueError, match="limit"):
        SearchQuery(query="a", limit=0)
    with pytest.raises(ValueError, match="offset"):
        SearchQuery(query="a", offset=-1)


def test_search_needs_the_token_and_a_same_origin_page(anonymous: TestClient, token: str) -> None:
    assert anonymous.post(URL, json={"query": "a"}, headers={"Origin": LOCAL}).status_code == 401
    other = anonymous.post(
        URL,
        json={"query": "a"},
        headers={"Authorization": f"Bearer {token}", "Origin": "http://evil.example"},
    )
    assert other.status_code == 403


def test_without_a_vault_there_is_no_search() -> None:
    app = create_app(Settings())
    client = TestClient(
        app, base_url=LOCAL, headers={"Authorization": f"Bearer {app.state.token}", "Origin": LOCAL}
    )
    assert client.post(URL, json={"query": "a"}).status_code == 503


def test_an_empty_query_does_not_touch_the_database(app: FastAPI) -> None:
    engine: Engine = app.state.engine
    assert search(engine, SearchQuery(query="  ")).total == 0


def test_a_section_path_edited_by_hand_is_a_fixed_error(app: FastAPI, library: Library) -> None:
    document_id = add(library, "edited.txt", "Edited by hand.")
    engine: Engine = app.state.engine
    with engine.begin() as connection:
        connection.execute(
            update(chunks)
            .where(chunks.c.document_id == document_id)
            .values(section_path=f'[1, "{CANARY}"]')
        )
    with pytest.raises(ValueError, match="section path") as error:
        search(engine, SearchQuery(query="edited"))
    assert CANARY not in str(error.value)


# --- the import date filter ---------------------------------------------------------------------


def add_at(library: Library, when: datetime, name: str, body: str) -> str:
    """A document whose first import is at ``when`` (naive UTC), to the microsecond. ``body``
    must differ from every other document's, or the import is a duplicate of that one."""
    library.now = when - timedelta(minutes=1)  # Library.add moves the clock on by a minute
    return add(library, name, body)


def test_import_bounds_are_inclusive_to_the_microsecond(
    client: TestClient, library: Library
) -> None:
    start = datetime(2026, 5, 4, 0, 0, 0)
    end = datetime(2026, 5, 4, 23, 59, 59, 999999)
    before = add_at(library, start - timedelta(microseconds=1), "a.txt", "Lease renewal a.")
    first = add_at(library, start, "b.txt", "Lease renewal b.")
    last = add_at(library, end, "c.txt", "Lease renewal c.")
    after = add_at(library, end + timedelta(microseconds=1), "d.txt", "Lease renewal d.")
    day = {"imported_from": "2026-05-04T00:00:00Z", "imported_to": "2026-05-04T23:59:59.999999Z"}
    assert sorted(found(client, "lease", **day)) == sorted([first, last])
    assert sorted(found(client, "lease", imported_from=day["imported_from"])) == sorted(
        [first, last, after]
    )
    assert sorted(found(client, "lease", imported_to=day["imported_to"])) == sorted(
        [before, first, last]
    )
    assert len(found(client, "lease")) == 4
    # A range of one instant holds what was imported at that instant.
    instant = "2026-05-04T23:59:59.999999Z"
    assert found(client, "lease", imported_from=instant, imported_to=instant) == [last]


def test_a_day_in_the_owners_zone_is_the_instants_the_page_sends(
    client: TestClient, library: Library
) -> None:
    # 8 March 2026 in New York is 23 hours long: it starts at 05:00 UTC (EST) and ends at 04:00
    # UTC the next day (EDT). The page sends those instants with the browser's offsets.
    late_on_the_7th = add_at(library, datetime(2026, 3, 8, 4, 59, 59, 999999), "a.md", "Rent due a")
    first = add_at(library, datetime(2026, 3, 8, 5, 0, 0), "b.md", "Rent due b")
    last = add_at(library, datetime(2026, 3, 9, 3, 59, 59, 999999), "c.md", "Rent due c")
    early_on_the_9th = add_at(library, datetime(2026, 3, 9, 4, 0, 0), "d.md", "Rent due d")
    local = {
        "imported_from": "2026-03-08T00:00:00-05:00",
        "imported_to": "2026-03-08T23:59:59.999999-04:00",
    }
    utc = {"imported_from": "2026-03-08T05:00:00Z", "imported_to": "2026-03-09T03:59:59.999999Z"}
    assert sorted(found(client, "rent", **local)) == sorted([first, last])
    assert sorted(found(client, "rent", **utc)) == sorted([first, last])
    # The same calendar day in UTC is a different set of documents.
    utc_day = {"imported_from": "2026-03-08T00:00:00Z", "imported_to": "2026-03-08T23:59:59.999Z"}
    assert sorted(found(client, "rent", **utc_day)) == sorted([late_on_the_7th, first])
    # East of UTC (UTC+14), the day starts on the previous UTC day.
    kiritimati = {
        "imported_from": "2026-03-09T00:00:00+14:00",
        "imported_to": "2026-03-09T23:59:59.999999+14:00",
    }
    assert sorted(found(client, "rent", **kiritimati)) == sorted([last, early_on_the_9th])


def test_the_import_date_is_the_first_import(client: TestClient, library: Library) -> None:
    document_id = add_at(library, datetime(2026, 6, 1, 12, 0), "a.txt", "Warranty card.")
    # The same bytes again a month later: a new source, but the document keeps its date.
    library.now = datetime(2026, 7, 1, 12, 0)
    assert library.add("copy.txt", "Warranty card.") == document_id
    june = {"imported_from": "2026-06-01T00:00:00Z", "imported_to": "2026-06-30T23:59:59Z"}
    july = {"imported_from": "2026-07-01T00:00:00Z", "imported_to": "2026-07-31T23:59:59Z"}
    assert found(client, "warranty", **june) == [document_id]
    assert found(client, "warranty", **july) == []


def test_an_empty_range_finds_nothing(client: TestClient, library: Library) -> None:
    add_at(library, datetime(2026, 4, 1, 8, 0), "a.txt", "Tax return.")
    empty = {"imported_from": "2026-04-02T00:00:00Z", "imported_to": "2026-04-30T23:59:59Z"}
    assert find(client, "tax", **empty) == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_totals_ranking_and_pages_see_only_the_range(client: TestClient, library: Library) -> None:
    inside = []
    for number in range(6):
        document_id = add_at(
            library,
            datetime(2026, 8, 1 + number, 10, 0),
            f"memo-{number}.txt",
            "Memo " * (number + 1),
        )
        if 2 <= number <= 4:
            inside.append(document_id)
    week = {"imported_from": "2026-08-03T00:00:00Z", "imported_to": "2026-08-05T23:59:59Z"}
    everything = find(client, "memo", limit=50, **week)
    assert everything["total"] == 3
    order = [item["document_id"] for item in everything["items"]]
    assert sorted(order) == sorted(inside)
    pages = [find(client, "memo", limit=2, offset=offset, **week) for offset in (0, 2)]
    assert [item["document_id"] for p in pages for item in p["items"]] == order
    assert all(p["total"] == 3 for p in pages)
    # With the type filter and the trash, in the same statement.
    library.trash(inside[0])
    assert sorted(found(client, "memo", types=["txt"], **week)) == sorted(inside[1:])
    assert found(client, "memo", types=["md"], **week) == []


def test_the_whole_representable_range_is_accepted(client: TestClient, library: Library) -> None:
    document_id = add(library, "a.txt", "Insurance policy.")
    widest = {
        "imported_from": "0001-01-01T00:00:00Z",
        "imported_to": "9999-12-31T23:59:59.999999Z",
    }
    assert found(client, "insurance", **widest) == [document_id]
    assert found(client, "insurance", imported_from="0001-01-01T00:00:00-23:59") == [document_id]
    assert found(client, "insurance", imported_to="9999-12-31T23:59:59.999999+23:59") == [
        document_id
    ]


BAD_INSTANTS: list[Any] = [
    CANARY,
    f"2026-05-04T00:00:00Z{CANARY}",
    "2026-05-04",  # a day: whose midnight?
    "2026-05-04T00:00:00",  # no offset: the server's zone is not the owner's
    "2026-05-04 00:00:00Z",
    "2026-05-04t00:00:00z",
    "2026-05-04T00:00:00Z\n",
    "2026-05-04T00:00:00Z ",
    " 2026-05-04T00:00:00Z",
    "2026-05-04T00:00:00+0500",
    "2026-05-04T00:00:00+05",
    "2026-05-04T00:00:00+24:00",
    "2026-05-04T00:00:00+05:60",
    "2026-05-04T00:00Z",
    "2026-05-04T00:00:00.1234567Z",  # finer than the database stores
    "2026-05-04T00:00:00.Z",
    "2026-02-29T00:00:00Z",  # not a leap year
    "2026-04-31T00:00:00Z",
    "2026-05-04T24:00:00Z",
    "2026-05-04T23:59:60Z",  # a leap second
    "0000-01-01T00:00:00Z",
    "0001-01-01T00:00:00+00:01",  # before the year 1 in UTC
    "9999-12-31T23:59:59.999999-00:01",  # after the year 9999 in UTC
    "10000-01-01T00:00:00Z",
    "+002026-05-04T00:00:00Z",
    "\uff12\uff10\uff12\uff16-05-04T00:00:00Z",  # full-width digits
    "\u0662\u0660\u0662\u0666-05-04T00:00:00Z",  # Arabic-Indic digits
    "",
    # Not strings: the framework's own refusal would repeat these.
    20260504,
    True,
    [CANARY],
    {"at": CANARY},
]


def test_bad_bounds_are_refused_without_repeating_them(
    client: TestClient, library: Library, caplog: pytest.LogCaptureFixture
) -> None:
    add(library, "a.txt", "Lease renewal a.")
    caplog.set_level(logging.DEBUG)
    for field in ("imported_from", "imported_to"):
        for value in BAD_INSTANTS:
            response = client.post(URL, json={"query": "lease", field: value})
            assert response.status_code == 422, (field, value)
            assert CANARY not in response.text
            assert response.json() == {
                "detail": f"{field} is not a time with a UTC offset between the years 1 and 9999"
            }
            # Refused before the query is read, so a query with nothing to match is no way past.
            assert client.post(URL, json={"query": "-", field: value}).status_code == 422
    assert CANARY not in caplog.text


def test_a_start_after_the_end_is_refused(client: TestClient) -> None:
    for start, end in (
        ("2026-05-04T00:00:00.000001Z", "2026-05-04T00:00:00Z"),
        # The same instant written two ways is not reversed; one microsecond later is.
        ("2026-05-04T00:00:00.000001-05:00", "2026-05-04T05:00:00Z"),
    ):
        response = client.post(URL, json={"query": "a", "imported_from": start, "imported_to": end})
        assert response.status_code == 422
        assert response.json() == {"detail": "imported_from is after imported_to"}
    same = {"imported_from": "2026-05-04T00:00:00-05:00", "imported_to": "2026-05-04T05:00:00Z"}
    assert client.post(URL, json={"query": "a", **same}).status_code == 200


def test_instants_become_naive_utc_by_arithmetic() -> None:
    assert parse_instant("2026-03-08T00:00:00-05:00", "f") == datetime(2026, 3, 8, 5, 0)
    assert parse_instant("2026-03-09T00:00:00+14:00", "f") == datetime(2026, 3, 8, 10, 0)
    assert parse_instant("2026-03-08T00:00:00.5Z", "f") == datetime(2026, 3, 8, 0, 0, 0, 500000)
    assert parse_instant("2026-03-08T00:00:00.000007Z", "f") == datetime(2026, 3, 8, 0, 0, 0, 7)
    assert parse_instant("2026-03-08T00:00:00-00:00", "f") == datetime(2026, 3, 8)
    # Past the year 3000, where Windows' C library stops.
    assert parse_instant("3001-01-01T00:00:00+01:00", "f") == datetime(3000, 12, 31, 23, 0)
    with pytest.raises(ValueError, match=r"^f is not") as error:
        parse_instant(CANARY, "f")
    assert CANARY not in str(error.value)


def test_search_query_bounds_are_checked() -> None:
    with pytest.raises(ValueError, match="naive UTC"):
        SearchQuery(query="a", imported_from=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="naive UTC"):
        SearchQuery(query="a", imported_to=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="after"):
        SearchQuery(query="a", imported_from=datetime(2026, 1, 2), imported_to=datetime(2026, 1, 1))
    SearchQuery(query="a", imported_from=datetime(2026, 1, 1), imported_to=datetime(2026, 1, 1))
