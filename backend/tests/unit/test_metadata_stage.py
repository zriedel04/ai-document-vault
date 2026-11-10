"""The metadata stage: a DOCX's core properties, read in the sandbox and judged in the server,
become the document's ``file_properties`` fields and its title."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import logging
import random
import re
import typing
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import docx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from tests.unit.test_docx_extractor import build, members_of, replace_member
from vault.api.documents import FieldSource
from vault.api.documents import FileProperties as ApiProperties
from vault.db import ids
from vault.db.fields import NAMES, FileProperties, file_properties_of, replace_file_properties
from vault.db.schema import (
    DATE_FIELDS,
    FIELD_NAMES,
    FIELD_SOURCES,
    MAX_FIELD_CHARS,
    document_fields,
    document_versions,
    documents,
    jobs,
)
from vault.ingest import metadata_stage
from vault.ingest.extractors import CoreProperties
from vault.ingest.extractors.docx import NotDocxError, read_properties
from vault.ingest.importer import Importer, ImportRequest, Outcome
from vault.ingest.metadata_stage import (
    METADATA_STAGE,
    BlobChangedError,
    PropertiesResultError,
    clean_text,
    parse_w3cdtf,
    properties_from,
    queue_pending,
    read_document_properties,
)
from vault.ingest.validate import EXTENSIONS
from vault.jobs import sandbox
from vault.jobs.queue import Completion, Job, JobQueue
from vault.jobs.sandbox import MIB, SandboxLimits
from vault.storage.blobs import BlobStore

LIMITS = SandboxLimits(timeout_seconds=120, memory_bytes=1024 * MIB)
NOW = datetime(2026, 10, 20, 9, 0, 0)
# Synthetic words only a test document holds, to look for in the logs.
CANARY = "canary-properties-xyzzy"
NAMESPACES = (
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:dcterms="http://purl.org/dc/terms/"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
)
W3CDTF = 'xsi:type="dcterms:W3CDTF"'
NONE = CoreProperties(title=None, creator=None, created=None, modified=None)


def core(inner: str, namespaces: str = NAMESPACES) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f"<cp:coreProperties {namespaces}>{inner}</cp:coreProperties>"
    ).encode()


def with_core(inner: str, namespaces: str = NAMESPACES) -> bytes:
    """A DOCX made by python-docx whose core properties part holds ``inner``."""
    return replace_member(build(), "docProps/core.xml", core(inner, namespaces))


def without(data: bytes, rewrite: Callable[[str, bytes], bytes | None]) -> bytes:
    """``data`` with each member passed through ``rewrite`` (None leaves it out)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members_of(data):
            changed = rewrite(name, content)
            if changed is not None:
                archive.writestr(name, changed)
    return out.getvalue()


def full_properties() -> str:
    return (
        f"<dc:title>Lab report {CANARY}</dc:title>"
        "<dc:creator>Sam Example</dc:creator>"
        f"<dcterms:created {W3CDTF}>2026-03-04T05:06:07Z</dcterms:created>"
        f"<dcterms:modified {W3CDTF}>2026-03-05T10:00:00+02:00</dcterms:modified>"
    )


# --- dates ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("2026", "2026"),
        ("2026-03", "2026-03"),
        ("2026-03-04", "2026-03-04"),
        ("2024-02-29", "2024-02-29"),
        ("2026-03-04T05:06Z", "2026-03-04T05:06:00Z"),
        ("2026-03-04T05:06:07Z", "2026-03-04T05:06:07Z"),
        ("2026-03-04T05:06:07.123456789Z", "2026-03-04T05:06:07Z"),
        ("2026-03-04T05:06:07+02:00", "2026-03-04T03:06:07Z"),
        ("2026-03-04T23:30:00-01:00", "2026-03-05T00:30:00Z"),
        ("2026-03-04T05:06:07-00:00", "2026-03-04T05:06:07Z"),
        ("2026-12-31T23:00:00-14:00", "2027-01-01T13:00:00Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
        ("0042", "0042"),
        (" \t\r\n2026-03-04T05:06:07Z\n ", "2026-03-04T05:06:07Z"),  # XML white space
    ],
)
def test_a_w3cdtf_date_is_stored_in_one_form(raw: str, stored: str) -> None:
    assert parse_w3cdtf(raw) == stored


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "26",
        "20260",
        "2026-3",
        "2026-03-4",
        "0000",
        "0000-01",
        "0000-01-01",
        "2026-00",
        "2026-13",
        "2026-02-29",
        "2026-04-31",
        "2026-03-04T05:06:07",  # a time without its zone
        "2026-03-04T05Z",
        "2026-03-04T5:06Z",
        "2026-03-04T24:00:00Z",
        "2026-03-04T05:60:00Z",
        "2026-03-04T05:06:60Z",  # a leap second
        "2026-03-04T05:06:07.Z",
        "2026-03-04T05:06:07+15:00",
        "2026-03-04T05:06:07+14:01",
        "2026-03-04T05:06:07+05:60",
        "2026-03-04T05:06:07+0200",
        "2026-03-04T05:06:07z",
        "2026-03-04t05:06:07Z",
        "2026-03-04 05:06:07Z",
        "2026-03-04T05:06:07ZZ",
        "+2026-03-04",
        "12026-03-04",
        "9999-12-31T23:00:00-02:00",  # UTC is in the year 10000
        "0001-01-01T00:30:00+01:00",  # UTC is in the year 0
        "\u0662\u0660\u0662\u0666",  # Arabic-Indic digits: \d would take them
        "\uff12\uff10\uff12\uff16-03-04",  # fullwidth digits
        "2026-03-04\u00a0",  # a no-break space is not XML white space
        "2026-03-04\x00",
        "2026-03-04T05:06:07." + "1" * 60 + "Z",  # longer than MAX_DATE_CHARS
        "2026-03-04/2026-03-05",
        "Tuesday",
        f"{CANARY}",
    ],
)
def test_anything_else_is_refused(raw: str) -> None:
    assert parse_w3cdtf(raw) is None


def test_the_date_rule_never_raises_on_random_text() -> None:
    """It is run on what a hostile file wrote: every string gives a value or None."""
    alphabet = "0123456789-:TZ+.\n \x00z\u0660"
    chance = random.Random(20261020)  # noqa: S311 - test inputs, not secrets
    for _ in range(20_000):
        text = "".join(chance.choice(alphabet) for _ in range(chance.randrange(0, 30)))
        result = parse_w3cdtf(text)
        assert result is None or re.fullmatch(
            r"[0-9]{4}(-[0-9]{2}(-[0-9]{2}(T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)?)?)?", result
        )


# --- names ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("Lab report", "Lab report"),
        ("  Lab\t\nreport  ", "Lab report"),
        ("Cafe\u0301", "Caf\u00e9"),  # NFC
        ("Invoice\u202egpj.exe", "Invoicegpj.exe"),  # a right-to-left override
        ("Zero\u200bwidth\ufeff", "Zerowidth"),
        ("Bell\x07 and\x00 null", "Bell and null"),
        ("\u5c65\u6b74\u66f8 \U0002000b", "\u5c65\u6b74\u66f8 \U0002000b"),
        ("2026", "2026"),
        ("", None),
        (" \u200b\u202e\x00 ", None),
    ],
)
def test_a_name_is_cleaned_as_a_folder_label_is(raw: str, stored: str | None) -> None:
    assert clean_text(raw) == stored


def test_a_long_name_is_cut_without_trailing_space() -> None:
    assert clean_text("a" * 1000) == "a" * MAX_FIELD_CHARS
    cut = clean_text("a" * (MAX_FIELD_CHARS - 1) + " bcd")
    assert cut == "a" * (MAX_FIELD_CHARS - 1)


def test_an_answer_is_judged_on_the_server_side() -> None:
    raw = CoreProperties(
        title=" Lab\u202e report ",
        creator="\x00",
        created="2026-03-04T05:06:07+01:00",
        modified="yesterday",
    )
    assert properties_from(raw) == FileProperties(
        title="Lab report", author=None, created="2026-03-04T04:06:07Z", modified=None
    )
    assert properties_from(NONE) == FileProperties()


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "title",
        {"title": "x", "creator": None, "created": None, "modified": None},
        FileProperties(title="x"),
        CoreProperties(title=1, creator=None, created=None, modified=None),  # type: ignore[arg-type]
        CoreProperties(title=None, creator=b"x", created=None, modified=None),  # type: ignore[arg-type]
    ],
)
def test_an_answer_of_another_shape_is_refused(answer: object) -> None:
    with pytest.raises(PropertiesResultError):
        properties_from(answer)


# --- reading the DOCX ----------------------------------------------------------------------------


def test_the_core_properties_are_read_as_written() -> None:
    assert read_properties(with_core(full_properties())) == CoreProperties(
        title=f"Lab report {CANARY}",
        creator="Sam Example",
        created="2026-03-04T05:06:07Z",
        modified="2026-03-05T10:00:00+02:00",
    )


def test_properties_written_by_python_docx_are_read() -> None:
    document = docx.Document()
    document.core_properties.title = "Syllabus"
    document.core_properties.author = "Sam"
    document.core_properties.created = datetime(2026, 1, 2, 3, 4, 5)
    out = io.BytesIO()
    document.save(out)
    read = read_properties(out.getvalue())
    assert (read.title, read.creator, read.created) == ("Syllabus", "Sam", "2026-01-02T03:04:05Z")


def test_missing_elements_are_none_and_an_empty_one_is_empty() -> None:
    assert read_properties(with_core("<dc:title/>")) == dataclasses.replace(NONE, title="")


def test_a_file_without_core_properties_has_none_and_none_are_made_up() -> None:
    """python-docx makes up a part (the title "Word Document") when asked for properties a file
    does not have; the reader never asks."""

    def drop(name: str, content: bytes) -> bytes | None:
        if name == "docProps/core.xml":
            return None
        if name == "_rels/.rels":
            return re.sub(rb"<Relationship [^>]*core-properties[^>]*/>", b"", content)
        return content

    data = without(with_core(full_properties()), drop)
    assert b"core-properties" not in dict(members_of(data))["_rels/.rels"]
    assert read_properties(data) == NONE


def test_a_core_part_of_another_content_type_is_not_read() -> None:
    def retype(name: str, content: bytes) -> bytes:
        if name == "[Content_Types].xml":
            return content.replace(
                b"application/vnd.openxmlformats-package.core-properties+xml", b"application/xml"
            )
        return content

    data = without(with_core(full_properties()), retype)
    assert read_properties(data) == NONE


def test_a_root_that_is_not_core_properties_is_not_read() -> None:
    data = replace_member(
        build(), "docProps/core.xml", f"<dc:title {NAMESPACES}>x</dc:title>".encode()
    )
    assert read_properties(data) == NONE


@pytest.mark.parametrize(
    "inner",
    [
        "<dc:title>One</dc:title><dc:title>Two</dc:title>",  # which one is the title?
        "<dc:title>One<dc:subject>Two</dc:subject></dc:title>",
        "<dc:title>One<!-- a comment -->Two</dc:title>",
        "<dc:title>One<?pi x?>Two</dc:title>",
    ],
)
def test_a_title_that_is_not_one_plain_text_is_left_out(inner: str) -> None:
    assert read_properties(with_core(inner)).title is None


@pytest.mark.parametrize(
    ("element", "namespaces", "read"),
    [
        (f"<dcterms:created {W3CDTF}>2026</dcterms:created>", NAMESPACES, "2026"),
        (
            '<dcterms:created xsi:type=" dcterms:W3CDTF ">2026</dcterms:created>',
            NAMESPACES,
            "2026",
        ),
        (  # another prefix for the same namespace, declared where it is used
            '<dcterms:created xmlns:t="http://purl.org/dc/terms/" xsi:type="t:W3CDTF">'
            "2026</dcterms:created>",
            NAMESPACES,
            "2026",
        ),
        ("<dcterms:created>2026</dcterms:created>", NAMESPACES, None),
        ('<dcterms:created xsi:type="W3CDTF">2026</dcterms:created>', NAMESPACES, None),
        ('<dcterms:created xsi:type="dc:W3CDTF">2026</dcterms:created>', NAMESPACES, None),
        ('<dcterms:created xsi:type="dcterms:Period">2026</dcterms:created>', NAMESPACES, None),
        ('<dcterms:created xsi:type="nope:W3CDTF">2026</dcterms:created>', NAMESPACES, None),
        (  # xsi:type in another namespace is not xsi:type
            '<dcterms:created xmlns:x="urn:example" x:type="dcterms:W3CDTF">2026</dcterms:created>',
            NAMESPACES,
            None,
        ),
        (
            f"<dcterms:created {W3CDTF}>2026</dcterms:created>"
            f"<dcterms:created {W3CDTF}>2027</dcterms:created>",
            NAMESPACES,
            None,
        ),
    ],
)
def test_a_date_must_say_it_is_w3cdtf(element: str, namespaces: str, read: str | None) -> None:
    assert read_properties(with_core(element, namespaces)).created == read


def test_elements_of_another_namespace_are_not_the_properties() -> None:
    inner = (
        '<x:title xmlns:x="urn:example">Wrong</x:title>'
        "<cp:creator>Wrong</cp:creator>"
        f"<cp:created {W3CDTF}>2026</cp:created>"
    )
    assert read_properties(with_core(inner)) == NONE


def test_a_file_that_is_not_a_readable_docx_is_refused() -> None:
    with pytest.raises(NotDocxError):
        read_properties(b"not a zip at all")
    with pytest.raises(NotDocxError):
        read_properties(replace_member(build(), "docProps/core.xml", b"<cp:coreProperties"))


def test_xml_entities_are_not_expanded() -> None:
    """A document type declaration that defines entities is not followed (python-docx's parser
    resolves no entities), so a billion laughs or an outside file cannot reach the title: the
    unresolved references are children of the element, and such a title is left out."""
    hostile = (
        b"<?xml version='1.0'?><!DOCTYPE cp:coreProperties ["
        b'<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY outside SYSTEM "file:///etc/hostname">]>'
        + core("<dc:title>&b;&outside;</dc:title><dc:creator>Sam</dc:creator>").split(b"?>", 1)[1]
    )
    data = replace_member(build(), "docProps/core.xml", hostile)
    assert read_properties(data) == dataclasses.replace(NONE, creator="Sam")


# --- the database ---------------------------------------------------------------------------------


def test_the_fields_and_the_schema_name_the_same_values() -> None:
    assert set(NAMES) == set(FIELD_NAMES)
    assert set(typing.get_args(FieldSource)) == {*FIELD_SOURCES, "filename"}
    assert set(FileProperties.__dataclass_fields__) == set(ApiProperties.model_fields)
    assert set(DATE_FIELDS) <= set(FIELD_NAMES)
    assert set(EXTENSIONS.values()) >= metadata_stage.EXTENSIONS
    assert set(metadata_stage.READERS.values()) <= set(sandbox.ENTRY_POINTS)


class Vault:
    """The test vault's database and store, with a helper that imports synthetic files."""

    def __init__(self, app: FastAPI, folder: Path) -> None:
        self.engine: Engine = app.state.engine
        self.store: BlobStore = app.state.blobs
        self.folder = folder
        self.queue = JobQueue(self.engine, clock=lambda: NOW)
        self.importer = Importer(self.engine, self.store, clock=lambda: NOW)

    def add(self, data: bytes, name: str = "Lab_report_3.docx") -> tuple[str, str]:
        """Import ``data``; the document's id and the SHA-256 of its bytes."""
        path = self.folder / f"incoming-{ids.new_ulid()}{Path(name).suffix}"
        path.write_bytes(data)
        result = self.importer.import_file(
            ImportRequest(
                path=path, original_filename=name, method="upload", batch_id=ids.new_ulid()
            )
        )
        assert result.outcome is Outcome.IMPORTED, result.reason
        assert result.document_id is not None
        return result.document_id, hashlib.sha256(data).hexdigest()

    def stages(self, document_id: str) -> list[str]:
        with self.engine.connect() as connection:
            return sorted(
                connection.execute(
                    select(jobs.c.stage).where(jobs.c.document_id == document_id)
                ).scalars()
            )

    def job(self, document_id: str) -> tuple[str, str | None, int]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(jobs.c.status, jobs.c.last_error, jobs.c.attempts).where(
                    jobs.c.document_id == document_id, jobs.c.stage == METADATA_STAGE
                )
            ).one()
        return row.status, row.last_error, row.attempts

    def claim(self) -> Job:
        job = self.queue.claim(ids.new_ulid(), (METADATA_STAGE,))
        assert job is not None
        return job

    def run(self, runner: Callable[..., object] = sandbox.run) -> Completion | None:
        handler = metadata_stage.handler(self.engine, self.queue, self.store, LIMITS, runner=runner)
        job = self.claim()
        try:
            return handler(job)
        except Exception as error:  # what the worker pool does with it
            self.queue.fail(job.id, job.worker_id, error)
            return None

    def fields(self, document_id: str) -> dict[str, str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(document_fields.c.field, document_fields.c.value).where(
                    document_fields.c.document_id == document_id
                )
            )
            return {row.field: row.value for row in rows}

    def version_of(self, document_id: str) -> str:
        with self.engine.connect() as connection:
            return connection.execute(
                select(documents.c.current_version_id).where(documents.c.id == document_id)
            ).scalar_one()


@pytest.fixture
def vault(app: FastAPI, tmp_path: Path) -> Vault:
    folder = tmp_path / "incoming"
    folder.mkdir()
    return Vault(app, folder)


def test_the_database_checks_what_a_field_holds(vault: Vault) -> None:
    document_id, _ = vault.add(build())
    version_id = vault.version_of(document_id)
    other_id, _ = vault.add(with_core("<dc:title>Other</dc:title>"))

    def row(**change: Any) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "field": "title",
            "source": "file_properties",
            "version_id": version_id,
            "value": "Lab report",
        } | change

    refused = [
        row(field="summary"),
        row(source="user"),
        row(value=""),
        row(value="x" * (MAX_FIELD_CHARS + 1)),
        row(version_id=None),
        row(version_id=vault.version_of(other_id)),  # another document's version
        row(version_id=ids.new_ulid()),
        row(field="created", value="yesterday"),
        row(field="created", value="2026-03-04T05:06:07+01:00"),
        row(field="modified", value="26"),
    ]
    for values in refused:
        with pytest.raises(IntegrityError), vault.engine.begin() as connection:
            connection.execute(insert(document_fields).values(values))
    accepted = [
        row(),
        row(field="author", value="2026"),  # a name may look like a year
        row(field="created", value="2026"),
        row(field="modified", value="2026-03-04T05:06:07Z"),
    ]
    for values in accepted:
        with vault.engine.begin() as connection:
            connection.execute(insert(document_fields).values(values))


def test_replacing_the_properties_replaces_every_field(vault: Vault) -> None:
    document_id, _ = vault.add(build())
    version_id = vault.version_of(document_id)
    with vault.engine.begin() as connection:
        replace_file_properties(
            connection, document_id, version_id, FileProperties(title="A", author="B")
        )
        replace_file_properties(connection, document_id, version_id, FileProperties(title="C"))
        assert file_properties_of(connection, document_id, version_id) == FileProperties(title="C")
        assert file_properties_of(connection, document_id, ids.new_ulid()) == FileProperties()
        with pytest.raises(ValueError, match="1 to 255"):
            replace_file_properties(connection, document_id, version_id, FileProperties(title=""))


# --- the stage ------------------------------------------------------------------------------------


def test_a_docx_gets_a_metadata_job_and_a_text_file_does_not(vault: Vault) -> None:
    docx_id, _ = vault.add(build())
    text_id, _ = vault.add(b"Plain notes\n", name="notes.txt")
    markdown_id, _ = vault.add(b"# Notes\n", name="notes.md")
    assert vault.stages(docx_id) == ["extract", "metadata"]
    assert vault.stages(text_id) == ["extract"]
    assert vault.stages(markdown_id) == ["extract"]


def test_the_properties_are_stored_and_give_the_title(
    vault: Vault,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    document_id, _ = vault.add(with_core(full_properties()))
    before = client.get(f"/api/documents/{document_id}").json()
    assert before["title"] == {"value": "Lab report 3", "source": "filename"}
    assert before["file_properties"] == {
        "title": None,
        "author": None,
        "created": None,
        "modified": None,
    }

    with caplog.at_level(logging.DEBUG):
        assert vault.run() is Completion.RECORDED

    assert vault.job(document_id) == ("done", None, 1)
    body = client.get(f"/api/documents/{document_id}").json()
    assert body["title"] == {"value": f"Lab report {CANARY}", "source": "file_properties"}
    assert body["file_properties"] == {
        "title": f"Lab report {CANARY}",
        "author": "Sam Example",
        "created": "2026-03-04T05:06:07Z",
        "modified": "2026-03-05T08:00:00Z",
    }
    assert body["name"] == "Lab_report_3.docx"  # the name is still the file's
    assert body["state"] == "processing"  # the stage leaves the state to extract and chunk
    out, err = capfd.readouterr()
    for text in (caplog.text, out, err):
        assert CANARY not in text
        assert "Sam Example" not in text


def test_malformed_properties_are_left_out_and_the_job_still_ends(
    vault: Vault, client: TestClient
) -> None:
    inner = (
        "<dc:title>\u202e\u200b </dc:title>"
        "<dc:creator>Sam\x09Example</dc:creator>"
        f"<dcterms:created {W3CDTF}>2026-02-30T00:00:00Z</dcterms:created>"
        f"<dcterms:modified {W3CDTF}>2026-03-04</dcterms:modified>"
    )
    document_id, _ = vault.add(with_core(inner))
    assert vault.run() is Completion.RECORDED
    assert vault.job(document_id) == ("done", None, 1)
    assert vault.fields(document_id) == {"author": "Sam Example", "modified": "2026-03-04"}
    body = client.get(f"/api/documents/{document_id}").json()
    assert body["title"] == {"value": "Lab report 3", "source": "filename"}


def test_a_long_title_is_bounded(vault: Vault) -> None:
    document_id, _ = vault.add(with_core(f"<dc:title>{'x' * 5000}</dc:title>"))
    assert vault.run() is Completion.RECORDED
    assert vault.fields(document_id) == {"title": "x" * MAX_FIELD_CHARS}


def test_an_unreadable_docx_fails_the_attempt_by_class_name(
    vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    """The reader raises in the child (python-docx cannot parse the core part, which the
    validator does not read); the server learns only that it raised, and the queue retries it."""
    broken = f"<cp:coreProperties {NAMESPACES}><dc:title>{CANARY}".encode()
    document_id, _ = vault.add(replace_member(build(), "docProps/core.xml", broken))
    with caplog.at_level(logging.DEBUG):
        assert vault.run() is None
    assert vault.job(document_id) == ("queued", "SandboxChildError", 1)
    assert vault.fields(document_id) == {}
    assert CANARY not in caplog.text


def test_an_answer_of_another_shape_fails_the_attempt(vault: Vault) -> None:
    document_id, _ = vault.add(build())
    assert vault.run(lambda *_args, **_kw: "not properties") is None
    assert vault.job(document_id) == ("queued", "PropertiesResultError", 1)


def test_a_blob_that_does_not_hold_its_bytes_is_never_read(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, _ = vault.add(with_core(full_properties()))
    real_open = vault.store.open

    def tampered(sha256: str) -> io.BytesIO:
        with real_open(sha256) as stream:
            data = bytearray(stream.read())
        data[-1] ^= 1
        return io.BytesIO(bytes(data))

    monkeypatch.setattr(vault.store, "open", tampered)
    called: list[object] = []
    assert vault.run(lambda *args, **_kw: called.append(args)) is None
    assert called == []
    assert vault.job(document_id) == ("queued", "BlobChangedError", 1)
    assert issubclass(BlobChangedError, Exception)


def test_a_job_for_bytes_the_document_no_longer_holds_stores_nothing(vault: Vault) -> None:
    document_id, sha256 = vault.add(with_core(full_properties()))
    other_id, other_sha = vault.add(with_core("<dc:title>Other</dc:title>"))
    # The document's current version now holds other bytes (a later phase's new version).
    version_id = ids.new_ulid()
    with vault.engine.begin() as connection:
        connection.execute(
            insert(document_versions).values(
                id=version_id,
                document_id=document_id,
                blob_sha256=other_sha,
                version_no=2,
                origin="user",
                created_at=NOW,
            )
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(current_version_id=version_id)
        )
        connection.execute(delete(jobs).where(jobs.c.document_id == other_id))
    assert (
        read_document_properties(
            vault.engine, vault.store, document_id, sha256, limits=LIMITS, runner=_never
        )
        is metadata_stage.Outcome.STALE
    )
    assert vault.fields(document_id) == {}
    assert (
        read_document_properties(
            vault.engine, vault.store, "not-an-id", sha256, limits=LIMITS, runner=_never
        )
        is metadata_stage.Outcome.STALE
    )


def test_a_job_that_names_no_document_ends_as_stale(vault: Vault) -> None:
    document_id, sha256 = vault.add(build())
    job = vault.claim()
    stale = dataclasses.replace(job, document_id="not-an-id")
    assert (
        read_document_properties(
            vault.engine,
            vault.store,
            stale.document_id,
            sha256,
            limits=LIMITS,
            finish=(vault.queue, stale),
            runner=_never,
        )
        is metadata_stage.Outcome.STALE
    )
    assert vault.job(document_id)[0] == "done"


class _Empty:
    """A snapshot in which the document's version is not found yet."""

    def __enter__(self) -> _Empty:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: object) -> _Empty:
        return self

    def first(self) -> None:
        return None


def test_a_version_that_keeps_appearing_after_the_snapshot_fails_the_attempt(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id, sha256 = vault.add(build())
    monkeypatch.setattr(metadata_stage, "read_snapshot", lambda _engine: _Empty())
    with pytest.raises(metadata_stage.VersionChangedError):
        read_document_properties(
            vault.engine, vault.store, document_id, sha256, limits=LIMITS, runner=_never
        )
    assert vault.run(_never) is Completion.ATTEMPT_QUEUED
    assert vault.job(document_id) == ("queued", "VersionChangedError", 1)


def _never(*_args: object, **_kw: object) -> object:
    raise AssertionError("the sandbox was called")


def test_the_properties_of_an_earlier_version_are_not_shown(
    vault: Vault, client: TestClient
) -> None:
    document_id, _ = vault.add(with_core(full_properties()))
    assert vault.run() is Completion.RECORDED
    _, other_sha = vault.add(b"New text\n", name="other.txt")
    with vault.engine.begin() as connection:
        version_id = ids.new_ulid()
        connection.execute(
            insert(document_versions).values(
                id=version_id,
                document_id=document_id,
                blob_sha256=other_sha,
                version_no=2,
                origin="user",
                created_at=NOW,
            )
        )
        connection.execute(
            update(documents)
            .where(documents.c.id == document_id)
            .values(current_version_id=version_id)
        )
    body = client.get(f"/api/documents/{document_id}").json()
    assert body["title"] == {"value": "Lab report 3", "source": "filename"}
    assert body["file_properties"]["author"] is None
    # A metadata job for a type with no reader stores none, and clears the old ones.
    vault.queue.enqueue(document_id, METADATA_STAGE, other_sha)
    assert vault.run(_never) is Completion.RECORDED
    assert vault.fields(document_id) == {}


def test_documents_from_before_the_stage_are_queued_once(vault: Vault) -> None:
    docx_id, sha256 = vault.add(with_core(full_properties()))
    text_id, _ = vault.add(b"Plain notes\n", name="notes.txt")
    failed_id, _ = vault.add(with_core("<dc:title>Failed</dc:title>"))
    with vault.engine.begin() as connection:
        connection.execute(
            delete(jobs).where(jobs.c.stage == METADATA_STAGE, jobs.c.document_id == docx_id)
        )
        connection.execute(
            update(jobs)
            .where(jobs.c.stage == METADATA_STAGE, jobs.c.document_id == failed_id)
            .values(status="failed", attempts=3, finished_at=NOW, last_error="SandboxChildError")
        )
    assert queue_pending(vault.engine, vault.queue) == 1
    assert queue_pending(vault.engine, vault.queue) == 0
    assert vault.stages(docx_id) == ["extract", "metadata"]
    assert vault.stages(text_id) == ["extract"]
    assert vault.job(failed_id)[0] == "failed"  # Retry's to run again, not a start-up's
    with vault.engine.connect() as connection:
        queued: str = connection.execute(
            select(jobs.c.input_hash).where(
                jobs.c.document_id == docx_id, jobs.c.stage == METADATA_STAGE
            )
        ).scalar_one()
    assert queued == sha256


def test_the_start_up_queues_them(tmp_path: Path) -> None:
    from vault.core.config import Settings
    from vault.main import create_app

    settings = Settings.model_validate({"vault": {"path": str(tmp_path / "vault")}})
    app = create_app(settings)
    folder = tmp_path / "incoming"
    folder.mkdir()
    first = Vault(app, folder)
    document_id, _ = first.add(build())
    with first.engine.begin() as connection:
        connection.execute(delete(jobs).where(jobs.c.stage == METADATA_STAGE))
    first.engine.dispose()
    again = create_app(settings)
    try:
        with again.state.engine.connect() as connection:
            count = connection.execute(
                select(func.count())
                .select_from(jobs)
                .where(jobs.c.document_id == document_id, jobs.c.stage == METADATA_STAGE)
            ).scalar_one()
        assert count == 1
    finally:
        again.state.engine.dispose()
