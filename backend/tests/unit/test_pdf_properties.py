"""The PDF properties reader (``vault.ingest.extractors.pdf.read_properties``), on synthetic PDFs
built in the tests: what PDFium gives for each entry of the document information dictionary, how
each text string form decodes, and which files are refused."""

from __future__ import annotations

import contextlib
import logging

import pypdfium2.raw as pdfium_c
import pytest

from tests.unit.pdf_builder import build, text_stream
from vault.ingest.extractors import ExtractionError, PdfProperties
from vault.ingest.extractors import pdf as extractor
from vault.ingest.extractors.pdf import (
    MAX_PROPERTY_UNITS,
    EncryptedPdfError,
    NotPdfError,
    read_properties,
)
from vault.ingest.validate import Limits, Reason
from vault.jobs import sandbox, sandbox_child
from vault.jobs.sandbox import EntryPoint, SandboxChildError, SandboxLimits

SECRET = "Confidential diagnosis"  # noqa: S105 (document text, not a password)
NONE = PdfProperties(title=None, author=None, created=None, modified=None)
SANDBOX_LIMITS = SandboxLimits(timeout_seconds=60, memory_bytes=512 * sandbox.MIB)

# PDFDocEncoding (ISO 32000-1, Annex D, table D.2), written out here from the standard rather than
# taken from PDFium: the codes where it differs from Latin-1, and the codes it leaves undefined.
PDF_DOC_ENCODING: dict[int, str] = {
    0x18: "\u02d8",  # breve
    0x19: "\u02c7",  # caron
    0x1A: "\u02c6",  # circumflex
    0x1B: "\u02d9",  # dot above
    0x1C: "\u02dd",  # double acute
    0x1D: "\u02db",  # ogonek
    0x1E: "\u02da",  # ring
    0x1F: "\u02dc",  # small tilde
    0x80: "\u2022",  # bullet
    0x81: "\u2020",  # dagger
    0x82: "\u2021",  # double dagger
    0x83: "\u2026",  # ellipsis
    0x84: "\u2014",  # em dash
    0x85: "\u2013",  # en dash
    0x86: "\u0192",  # florin
    0x87: "\u2044",  # fraction slash
    0x88: "\u2039",  # single left guillemet
    0x89: "\u203a",  # single right guillemet
    0x8A: "\u2212",  # minus
    0x8B: "\u2030",  # per mille
    0x8C: "\u201e",  # double low-9 quotation mark
    0x8D: "\u201c",  # left double quotation mark
    0x8E: "\u201d",  # right double quotation mark
    0x8F: "\u2018",  # left single quotation mark
    0x90: "\u2019",  # right single quotation mark
    0x91: "\u201a",  # single low-9 quotation mark
    0x92: "\u2122",  # trade mark
    0x93: "\ufb01",  # fi ligature
    0x94: "\ufb02",  # fl ligature
    0x95: "\u0141",  # L with stroke
    0x96: "\u0152",  # OE
    0x97: "\u0160",  # S with caron
    0x98: "\u0178",  # Y with diaeresis
    0x99: "\u017d",  # Z with caron
    0x9A: "\u0131",  # dotless i
    0x9B: "\u0142",  # l with stroke
    0x9C: "\u0153",  # oe
    0x9D: "\u0161",  # s with caron
    0x9E: "\u017e",  # z with caron
    0xA0: "\u20ac",  # euro
}
# Undefined in PDFDocEncoding; PDFium gives U+0000 for them, which the stage's cleaning removes.
UNDEFINED: frozenset[int] = frozenset({0x00, 0x7F, 0x9F, 0xAD})


def expected_pdf_doc(code: int) -> str:
    if code in UNDEFINED:
        return "\x00"
    # Every other code is Latin-1 (control codes 0x01 to 0x17 as themselves, as PDFium gives them).
    return PDF_DOC_ENCODING.get(code, chr(code))


def title_of(value: bytes) -> str | None:
    return read_properties(build([None], info={"Title": value})).title


# --- What is read --------------------------------------------------------------------------------


def test_the_four_entries_are_read_as_written_and_nothing_is_cleaned_or_parsed() -> None:
    data = build(
        [text_stream(["Body"])],
        info={
            "Title": b"  Lab\treport 3 ",
            "Author": b"Sam Example",
            "CreationDate": b"D:20260304050607+01'00'",
            "ModDate": b"not a date\n",
            "Subject": b"not read",
        },
    )

    assert read_properties(data) == PdfProperties(
        title="  Lab\treport 3 ",
        author="Sam Example",
        created="D:20260304050607+01'00'",
        modified="not a date\n",
    )
    assert extractor.PROPERTIES == {
        "title": "Title",
        "author": "Author",
        "created": "CreationDate",
        "modified": "ModDate",
    }
    assert set(extractor.PROPERTIES) == set(PdfProperties.__dataclass_fields__)


def test_a_file_without_an_info_dictionary_has_none() -> None:
    assert read_properties(build([text_stream(["Body"])])) == NONE


def test_an_empty_entry_is_none_not_an_empty_string() -> None:
    data = build([None], info={"Title": b"", "Author": b"\xfe\xff"})

    assert read_properties(data) == NONE


@pytest.mark.parametrize("code", range(256))
def test_every_code_of_pdf_doc_encoding_decodes_as_the_standard_says(code: int) -> None:
    # Each code after a letter, so that no code is read as a byte order mark.
    assert title_of(b"A" + bytes([code]) + b"Z") == f"A{expected_pdf_doc(code)}Z"


def test_the_whole_pdf_doc_encoding_table_in_one_string() -> None:
    every = bytes(range(1, 256)) + b"\x00"
    assert title_of(b"A" + every) == "A" + "".join(expected_pdf_doc(code) for code in every)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        pytest.param(b"\xfe\xff\x00H\x00i", "Hi", id="basic plane"),
        pytest.param(
            b"\xfe\xff\x00\xe9\x05\xd0\x4e\x2d", "\u00e9\u05d0\u4e2d", id="Latin Hebrew CJK"
        ),
        pytest.param(b"\xfe\xff\xd8\x3d\xde\x00", "\U0001f600", id="surrogate pair"),
        pytest.param(b"\xfe\xff\x00H\xd8\x3d\x00i", "H\ufffdi", id="lone high surrogate"),
        pytest.param(b"\xfe\xff\x00H\xde\x00\x00i", "H\ufffdi", id="lone low surrogate"),
        pytest.param(b"\xfe\xff\x00\x80", "\u0080", id="not PDFDocEncoding after the mark"),
    ],
)
def test_utf_16be_after_its_byte_order_mark_decodes(value: bytes, text: str) -> None:
    assert title_of(value) == text


def test_the_byte_order_mark_is_only_a_mark_at_the_start() -> None:
    # Later, FE FF are PDFDocEncoding's thorn and y with diaeresis, read as text.
    assert title_of(b"A\xfe\xff\x00B") == "A\u00fe\u00ff\x00B"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        pytest.param(b"\xef\xbb\xbfCaf\xc3\xa9", "Caf\u00e9", id="UTF-8 after its mark (PDF 2.0)"),
        pytest.param(b"\xff\xfeH\x00i\x00", "Hi", id="UTF-16LE after its mark"),
    ],
)
def test_the_marks_pdfium_takes_beyond_pdf_1_7(value: bytes, text: str) -> None:
    # Pinned so that a PDFium update that reads them otherwise is noticed.
    assert title_of(value) == text


@pytest.mark.parametrize(
    ("source", "title"),
    [
        pytest.param(b"<< /Title /Foo#20Bar >>", "Foo Bar", id="a name reads as its text"),
        pytest.param(b"<< /Title 42 >>", None, id="a number"),
        pytest.param(b"<< /Title [(a)] >>", None, id="an array"),
        pytest.param(b"<< /Title << /A (a) >> >>", None, id="a dictionary"),
        pytest.param(b"<< /Title true >>", None, id="a boolean"),
        pytest.param(b"<< /Title null >>", None, id="null"),
        pytest.param(b"<< /Title (one) /Title (two) >>", "two", id="written twice: the last"),
        pytest.param(b"<< /Title 9 0 R >>", None, id="a reference to nothing"),
        pytest.param(b"<< /Title (a\\(b\\)\\101\\n) >>", "a(b)A\n", id="literal with escapes"),
    ],
)
def test_what_pdfium_gives_for_other_values(source: bytes, title: str | None) -> None:
    assert read_properties(build([None], info_source=source)).title == title


def test_an_info_that_is_not_a_dictionary_gives_none() -> None:
    assert read_properties(build([None], info_source=b"(not a dictionary)")) == NONE


def test_a_value_of_exactly_the_bound_is_read_and_one_unit_more_is_none() -> None:
    assert title_of(b"x" * MAX_PROPERTY_UNITS) == "x" * MAX_PROPERTY_UNITS
    assert title_of(b"x" * (MAX_PROPERTY_UNITS + 1)) is None
    # The bound counts UTF-16 units, so a character beyond the basic plane counts twice.
    pair = b"\xd8\x3d\xde\x00"
    half = MAX_PROPERTY_UNITS // 2
    assert title_of(b"\xfe\xff" + pair * half) == "\U0001f600" * half
    assert title_of(b"\xfe\xff" + pair * half + b"\x00x") is None


def test_a_value_over_the_bound_is_never_copied_out_of_pdfium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = pdfium_c.FPDF_GetMetaText

    def recording(document: object, tag: bytes, buffer: object, size: int) -> int:
        calls.append(size)
        result: int = real(document, tag, buffer, size)
        return result

    monkeypatch.setattr(pdfium_c, "FPDF_GetMetaText", recording)
    data = build([None], info={"Title": b"x" * (MAX_PROPERTY_UNITS + 1)})

    assert read_properties(data).title is None
    assert calls == [0, 0, 0, 0]  # each entry's size was asked, and no value was copied


def test_the_answer_at_the_bound_fits_the_sandbox_defaults() -> None:
    """Four values at the bound, each of the character the answer's JSON writes longest
    (a ``\\uXXXX`` escape per unit), stay inside the sandbox's default cap on an answer."""
    worst = "\U0001f600" * (MAX_PROPERTY_UNITS // 2)
    answer = PdfProperties(title=worst, author=worst, created=worst, modified=worst)

    assert len(sandbox_child.encode(answer)) <= sandbox.DEFAULT_MAX_RESULT_BYTES


# --- Opened as the extractor opens it ------------------------------------------------------------


def test_a_pdf_with_an_owner_password_only_has_its_strings_decrypted() -> None:
    data = build(
        [None],
        user_password=b"",
        info={"Title": b"Decrypted title", "Author": b"\xfe\xff\x00S\x00a\x00m"},
    )

    assert read_properties(data) == PdfProperties(
        title="Decrypted title", author="Sam", created=None, modified=None
    )


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            build([None], user_password=b"secret", info={"Title": SECRET.encode()}),
            id="user password",
        ),
        pytest.param(
            build([None], user_password=b"", security_filter="Unknown", info={"Title": b"x"}),
            id="unsupported security handler",
        ),
    ],
)
def test_a_pdf_that_needs_a_password_is_refused(data: bytes) -> None:
    with pytest.raises(EncryptedPdfError):
        read_properties(data)


def test_no_password_is_ever_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    load = pdfium_c.FPDF_LoadMemDocument64

    def recording(data: bytes, size: int, password: object) -> object:
        calls.append(password)
        return load(data, size, password)

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", recording)
    with pytest.raises(EncryptedPdfError):
        read_properties(build([None], user_password=b"secret"))
    read_properties(build([None]))

    assert calls == [None, None]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"%PDF-1.4\n" + SECRET.encode(), id="garbage after the header"),
        pytest.param(build([None], info={"Title": SECRET.encode()})[:60], id="cut short"),
        pytest.param(b"%PDF-1.7\n" + b"\x00" * 4096, id="zeros"),
    ],
)
def test_a_pdf_pdfium_cannot_open_is_refused(data: bytes) -> None:
    with pytest.raises(NotPdfError) as caught:
        read_properties(data)

    assert caught.value.reason is None


@pytest.mark.parametrize(
    ("data", "limits", "reason"),
    [
        (b"", Limits(), Reason.EMPTY),
        (b"PK\x03\x04 not a pdf", Limits(), Reason.CONTENT_MISMATCH),
        (SECRET.encode(), Limits(), Reason.CONTENT_MISMATCH),
        (build([None], info={"Title": b"x"}), Limits(max_bytes=10), Reason.TOO_LARGE),
    ],
    ids=["empty", "zip", "text", "too large"],
)
def test_bytes_the_validator_refuses_never_reach_pdfium(
    data: bytes, limits: Limits, reason: Reason, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_args: object) -> None:
        raise AssertionError

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", never)

    with pytest.raises(NotPdfError) as caught:
        read_properties(data, limits)

    assert caught.value.reason is reason


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(build([], info={"Title": b"No pages"}), id="no pages"),
        pytest.param(
            build([text_stream(["x"])], claimed_pages=3, info={"Title": b"No pages"}),
            id="a page the tree claims but does not hold",
        ),
    ],
)
def test_the_pages_are_not_loaded(data: bytes) -> None:
    # The extractor refuses these files; reading the properties does not need their pages.
    assert read_properties(data).title == "No pages"


def test_an_error_quotes_nothing_and_keeps_no_library_exception() -> None:
    for data in (
        build([None], user_password=b"secret", info={"Title": SECRET.encode()}),
        b"%PDF-1.4\n" + SECRET.encode(),
    ):
        with pytest.raises(ExtractionError) as caught:
            read_properties(data)
        assert SECRET not in str(caught.value)
        assert caught.value.__cause__ is None


def test_nothing_reaches_the_console_or_the_log(
    capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for data in (
        build([None], info={"Title": SECRET.encode(), "CreationDate": b"D:2026" + b"\xff" * 9}),
        build([None], info_source=b"<< /Title (" + SECRET.encode() + b" >>"),
        b"%PDF-1.4\n" + SECRET.encode(),
    ):
        with contextlib.suppress(ExtractionError):
            read_properties(data)

    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    assert caplog.records == []


# --- In the sandbox ------------------------------------------------------------------------------


def test_it_runs_in_the_sandbox_as_the_pdf_properties_entry_point() -> None:
    assert sandbox.ENTRY_POINTS["pdf_properties"] == EntryPoint(
        "vault.ingest.extractors.pdf:read_properties", PdfProperties
    )
    data = build(
        [text_stream(["Body"])],
        info={"Title": b"\xfe\xff\x00I\x00n\x00 \xd8\x3d\xde\x00", "ModDate": b"D:2026"},
    )

    result = sandbox.run("pdf_properties", data, SANDBOX_LIMITS)

    assert result == PdfProperties(
        title="In \U0001f600", author=None, created=None, modified="D:2026"
    )


@pytest.mark.parametrize(
    ("data", "error_class"),
    [
        (build([None], user_password=b"pw", info={"Title": SECRET.encode()}), "EncryptedPdfError"),
        (b"%PDF-1.4\n" + SECRET.encode(), "NotPdfError"),
    ],
    ids=["encrypted", "corrupt"],
)
def test_a_refusal_in_the_sandbox_comes_back_as_its_class_name(
    data: bytes, error_class: str
) -> None:
    with pytest.raises(SandboxChildError) as caught:
        sandbox.run("pdf_properties", data, SANDBOX_LIMITS)

    assert caught.value.error_class == error_class
    assert SECRET not in str(caught.value)
