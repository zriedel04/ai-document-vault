"""The PDF extractor (``vault.ingest.extractors.pdf``), on synthetic PDFs built in the tests."""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path

import pypdfium2
import pypdfium2.raw as pdfium_c
import pytest

from tests.unit.pdf_builder import build, text_stream, to_unicode_map
from vault.ingest.extractors import Extraction, ExtractionError, Page
from vault.ingest.extractors import pdf as extractor
from vault.ingest.extractors.pdf import (
    EmptyPdfError,
    EncryptedPdfError,
    NotPdfError,
    PdfLimitError,
    extract,
)
from vault.ingest.validate import DEFAULT_LIMITS, Limits, Reason
from vault.jobs import sandbox, sandbox_child
from vault.jobs.sandbox import EntryPoint, SandboxChildError, SandboxLimits

# Words that stand for a document's private content and its file name: no error, log line or
# console output may hold them.
SECRET = "Confidential diagnosis"  # noqa: S105 (document text, not a password)
FILE_NAME = "Patient-Name.pdf"
ENTRY = "vault.ingest.extractors.pdf:extract"


def texts(data: bytes, limits: Limits = DEFAULT_LIMITS) -> list[str]:
    return [page.text for page in extract(data, limits).pages]


# --- What the text holds -------------------------------------------------------------------------


def test_one_page_per_pdf_page_in_order_with_the_version() -> None:
    data = build([text_stream(["First page"]), text_stream(["Second page"])])

    result = extract(data)

    assert result == Extraction(
        "pdf-1", (Page(1, "First page"), Page(2, "Second page")), encoding=None
    )
    assert all(page.headings == () for page in result.pages)


def test_line_breaks_are_newlines() -> None:
    assert texts(build([text_stream(["one", "two", "three"])])) == ["one\ntwo\nthree"]


def test_a_word_hyphenated_at_a_line_end_is_joined() -> None:
    # PDFium marks the hyphen with U+FFFE and drops the line break after it.
    assert texts(build([text_stream(["An exam-", "ple here"])])) == ["An example here"]


def test_a_hyphen_inside_a_line_is_kept() -> None:
    assert texts(build([text_stream(["self-made plans"])])) == ["self-made plans"]


def test_a_page_with_no_text_layer_is_an_empty_page() -> None:
    data = build([text_stream(["Typed"]), None, text_stream(["Typed again"])])

    assert texts(data) == ["Typed", "", "Typed again"]


@pytest.mark.parametrize("lines", [[" "], ["   ", "  "], ["\t"]])
def test_a_page_of_only_white_space_is_an_empty_page(lines: list[str]) -> None:
    assert texts(build([text_stream(lines)])) == [""]


def test_a_scan_with_no_text_on_any_page_is_still_read_as_empty_pages() -> None:
    assert texts(build([None, None])) == ["", ""]


def test_characters_beyond_the_basic_plane_come_through() -> None:
    mapping = to_unicode_map({0x41: "D83DDE00", 0x42: "00E9", 0x43: "4E2D"})

    assert texts(build([text_stream(["ABC"])], to_unicode=mapping)) == ["\U0001f600é中"]


def test_characters_that_are_not_text_are_removed() -> None:
    # A broken font map can give NUL and other control characters (PDFium itself replaces a map
    # to U+FFFF with the character code, so that one is covered by test_clean).
    mapping = to_unicode_map({0x41: "0041", 0x42: "0000", 0x43: "0007", 0x44: "001B", 0x45: "0085"})

    assert texts(build([text_stream(["ABCDEA"])], to_unicode=mapping)) == ["AA"]


def test_half_of_a_surrogate_pair_becomes_a_replacement_character() -> None:
    mapping = to_unicode_map({0x41: "0041", 0x42: "D83D"})

    (text,) = texts(build([text_stream(["ABA"])], to_unicode=mapping))

    assert "\ud83d" not in text
    assert text.startswith("A")
    assert text.endswith("A")
    text.encode("utf-8")  # storable: no lone surrogate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb\rc\nd", "a\nb\nc\nd"),
        ("exam￾ple", "example"),
        ("tab\tkept", "tab\tkept"),
        ("\x00\x01\x08\x0b\x0c\x0e\x1f\x7f\x80\x9f￿x", "x"),
        (" \r\n\t ", ""),
        ("　", ""),
        ("  text  ", "  text  "),
    ],
)
def test_clean(raw: str, expected: str) -> None:
    assert extractor.clean(raw) == expected


def test_every_text_pdf_of_the_demo_corpus_reads_as_its_reference_text() -> None:
    corpus = Path(__file__).resolve().parents[3] / "demo" / "corpus"
    references = corpus / "references"
    files = sorted(corpus.rglob("*.pdf"))
    text_pdfs = [
        p for p in files if (references / p.relative_to(corpus)).with_suffix(".txt").exists()
    ]
    scans = [p for p in files if p not in text_pdfs]
    assert text_pdfs
    assert scans

    for path in text_pdfs:
        result = extract(path.read_bytes())
        reference = (references / path.relative_to(corpus)).with_suffix(".txt")
        words = " ".join(page.text for page in result.pages).split()
        assert words == reference.read_text(encoding="utf-8").split()
        assert all(page.text for page in result.pages)
    for path in scans:
        result = extract(path.read_bytes())
        assert result.pages
        assert all(page.text == "" for page in result.pages)


# --- Refused files -------------------------------------------------------------------------------


def test_a_pdf_that_needs_a_password_is_refused() -> None:
    data = build([text_stream([SECRET])], user_password=b"secret")

    with pytest.raises(EncryptedPdfError):
        extract(data)


def test_a_pdf_with_an_unsupported_security_handler_is_refused_as_encrypted() -> None:
    data = build([text_stream([SECRET])], user_password=b"", security_filter="Unknown")

    with pytest.raises(EncryptedPdfError):
        extract(data)


def test_no_password_is_ever_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    load = pdfium_c.FPDF_LoadMemDocument64

    def recording(data: bytes, size: int, password: object) -> object:
        calls.append(password)
        return load(data, size, password)

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", recording)
    with pytest.raises(EncryptedPdfError):
        extract(build([text_stream(["x"])], user_password=b"secret"))
    extract(build([text_stream(["x"])]))

    assert calls == [None, None]


def test_a_pdf_with_an_owner_password_only_is_read() -> None:
    # It opens without a password and only restricts what a viewer may do with it.
    data = build([text_stream(["Readable text"])], user_password=b"")

    assert texts(data) == ["Readable text"]


def test_a_pdf_with_no_pages_is_refused() -> None:
    with pytest.raises(EmptyPdfError):
        extract(build([]))


def test_no_pages_after_an_encrypted_file_is_still_no_pages() -> None:
    # PDFium keeps its last error after a document opens, so reading the error for a document
    # that opened with no pages would report the earlier file's password error.
    with pytest.raises(EncryptedPdfError):
        extract(build([text_stream(["x"])], user_password=b"secret"))

    with pytest.raises(EmptyPdfError):
        extract(build([]))


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"%PDF-1.4\n" + SECRET.encode(), id="garbage after the header"),
        pytest.param(build([text_stream([SECRET])])[:60], id="cut short"),
        pytest.param(b"%PDF-1.7\n" + b"\x00" * 4096, id="zeros"),
        pytest.param(
            build([text_stream([SECRET])]).replace(b"/Root 1 0 R", b"/Root 9 0 R"), id="no root"
        ),
    ],
)
def test_a_corrupt_pdf_is_refused(data: bytes) -> None:
    with pytest.raises(NotPdfError) as caught:
        extract(data)

    assert caught.value.reason is None


def test_a_page_the_tree_claims_but_does_not_hold_refuses_the_file() -> None:
    data = build([text_stream(["Only page"])], claimed_pages=3)

    with pytest.raises(NotPdfError):
        extract(data)


def test_a_page_whose_text_pdfium_cannot_load_refuses_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(_self: object) -> None:
        raise pypdfium2.PdfiumError(SECRET)

    monkeypatch.setattr(pypdfium2.PdfPage, "get_textpage", failing)

    with pytest.raises(NotPdfError) as caught:
        extract(build([text_stream(["x"])]))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", Reason.EMPTY),
        (b"PK\x03\x04 not a pdf", Reason.CONTENT_MISMATCH),
        (b"hello %PDF-1.4", Reason.CONTENT_MISMATCH),
        (SECRET.encode(), Reason.CONTENT_MISMATCH),
    ],
)
def test_bytes_that_are_not_a_pdf_are_refused_before_pdfium_sees_them(
    data: bytes, reason: Reason, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_args: object) -> None:
        raise AssertionError

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", never)

    with pytest.raises(NotPdfError) as caught:
        extract(data)

    assert caught.value.reason is reason


def test_bytes_over_the_size_limit_are_refused_before_pdfium_sees_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = build([text_stream(["x"])])

    def never(*_args: object) -> None:
        raise AssertionError

    monkeypatch.setattr(pdfium_c, "FPDF_LoadMemDocument64", never)

    with pytest.raises(NotPdfError) as caught:
        extract(data, Limits(max_bytes=len(data) - 1))

    assert caught.value.reason is Reason.TOO_LARGE


def test_the_header_may_follow_a_little_white_space() -> None:
    assert texts(b"\r\n" + build([text_stream(["after white space"])])) == ["after white space"]


# --- Limits --------------------------------------------------------------------------------------


def test_more_pages_than_the_limit_are_refused_before_any_page_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = build([text_stream(["a"]), text_stream(["b"]), text_stream(["c"])])

    def never(*_args: object) -> None:
        raise AssertionError

    monkeypatch.setattr(extractor, "_page_text", never)

    with pytest.raises(PdfLimitError) as caught:
        extract(data, Limits(max_pdf_pages=2))
    assert caught.value.limit == "pages"


def test_exactly_the_page_limit_is_read() -> None:
    data = build([text_stream(["a"]), text_stream(["b"])])

    assert texts(data, Limits(max_pdf_pages=2)) == ["a", "b"]


def test_more_text_than_the_limit_is_refused_before_the_page_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = build([text_stream(["abcde"]), text_stream(["fghij"])])
    read: list[int] = []
    original = pypdfium2.PdfTextPage.get_text_range

    def counting(self: pypdfium2.PdfTextPage, *args: object, **kwargs: object) -> str:
        read.append(1)
        return str(original(self, *args, **kwargs))

    monkeypatch.setattr(pypdfium2.PdfTextPage, "get_text_range", counting)

    with pytest.raises(PdfLimitError) as caught:
        extract(data, Limits(max_pdf_text_chars=9))
    assert caught.value.limit == "text"
    assert read == [1]  # the first page only: the second's count was over what was left


def test_exactly_the_text_limit_is_read() -> None:
    data = build([text_stream(["abcde"]), text_stream(["fghij"])])

    assert texts(data, Limits(max_pdf_text_chars=10)) == ["abcde", "fghij"]


def test_text_pdfium_adds_beyond_its_count_is_counted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    data = build([text_stream(["abc"])])

    def longer(_self: object, *_args: object, **_kwargs: object) -> str:
        return "abcdefgh"

    monkeypatch.setattr(pypdfium2.PdfTextPage, "get_text_range", longer)

    with pytest.raises(PdfLimitError):
        extract(data, Limits(max_pdf_text_chars=5))
    assert texts(data, Limits(max_pdf_text_chars=8)) == ["abcdefgh"]


@pytest.mark.parametrize(
    "character",
    [
        pytest.param(",", id="a value mark for the sandbox"),
        pytest.param("\U0001f600", id="twelve bytes of JSON"),
        pytest.param('"', id="an escaped quote"),
    ],
)
def test_the_largest_result_the_limits_allow_fits_the_sandbox_defaults(character: str) -> None:
    # The worst case for the sandbox's answer: every page there may be, the whole text cap
    # spent on the character that costs the most, and every page number at its widest.
    limits = DEFAULT_LIMITS
    per_page, rest = divmod(limits.max_pdf_text_chars, limits.max_pdf_pages)
    pages = tuple(
        Page(number, character * (per_page + (1 if number <= rest else 0)))
        for number in range(1, limits.max_pdf_pages + 1)
    )
    result = Extraction(extractor.VERSION, pages)
    status, payload = sandbox_child.OK, sandbox_child.encode(result)
    answer = status + payload

    assert len(answer) <= sandbox.DEFAULT_MAX_RESULT_BYTES
    cost, values = sandbox._answer_cost(answer)
    assert cost <= sandbox.DEFAULT_MAX_RESULT_MEMORY_BYTES
    assert values <= sandbox.DEFAULT_MAX_RESULT_VALUES
    decoded = sandbox._interpret(answer, EntryPoint(ENTRY, Extraction)._decoder)
    assert decoded == result


# --- Nothing private leaves the extractor --------------------------------------------------------


REFUSED = [
    pytest.param(build([text_stream([SECRET])], user_password=b"pw"), id="encrypted"),
    pytest.param(build([]), id="no pages"),
    pytest.param(b"%PDF-1.4\n" + SECRET.encode() + b"\n" + FILE_NAME.encode(), id="corrupt"),
    pytest.param(build([text_stream([SECRET])], claimed_pages=2), id="missing page"),
    pytest.param(SECRET.encode(), id="not a pdf"),
]


@pytest.mark.parametrize("data", REFUSED)
def test_an_error_quotes_nothing_and_keeps_no_library_exception(data: bytes) -> None:
    with pytest.raises(ExtractionError) as caught:
        extract(data)

    error = caught.value
    message = str(error)
    assert SECRET not in message
    assert FILE_NAME not in message
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_a_limit_error_quotes_nothing() -> None:
    with pytest.raises(PdfLimitError) as caught:
        extract(build([text_stream([SECRET])]), Limits(max_pdf_text_chars=3))

    assert SECRET not in str(caught.value)
    assert str(caught.value) == "The PDF is over the text limit."


@pytest.mark.parametrize("data", [*REFUSED, pytest.param(build([text_stream([SECRET])]), id="ok")])
def test_nothing_reaches_the_console_or_the_log(
    data: bytes, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with contextlib.suppress(ExtractionError):
        extract(data)

    out, err = capfd.readouterr()
    assert out == ""
    assert err == ""
    assert caplog.records == []


# --- Running it ----------------------------------------------------------------------------------


def test_calls_from_several_threads_give_each_its_own_result() -> None:
    documents = [build([text_stream([f"Document {n}"]) for _ in range(5)]) for n in range(8)]
    results: dict[int, list[str]] = {}

    def work(n: int) -> None:
        results[n] = texts(documents[n])

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == {n: [f"Document {n}"] * 5 for n in range(8)}


SANDBOX_ENTRIES = {"pdf": EntryPoint(ENTRY, Extraction)}
SANDBOX_LIMITS = SandboxLimits(timeout_seconds=60, memory_bytes=512 * sandbox.MIB)


def test_it_runs_in_the_sandbox() -> None:
    data = build([text_stream(["In the child"]), None])

    result = sandbox.run("pdf", data, SANDBOX_LIMITS, entry_points=SANDBOX_ENTRIES)

    assert result == Extraction("pdf-1", (Page(1, "In the child"), Page(2, "")))


@pytest.mark.parametrize(
    ("data", "error_class"),
    [
        (build([text_stream([SECRET])], user_password=b"pw"), "EncryptedPdfError"),
        (build([]), "EmptyPdfError"),
        (b"%PDF-1.4\n" + SECRET.encode(), "NotPdfError"),
    ],
    ids=["encrypted", "no pages", "corrupt"],
)
def test_a_refusal_in_the_sandbox_comes_back_as_its_class_name(
    data: bytes, error_class: str
) -> None:
    with pytest.raises(SandboxChildError) as caught:
        sandbox.run("pdf", data, SANDBOX_LIMITS, entry_points=SANDBOX_ENTRIES)

    assert caught.value.error_class == error_class
    assert SECRET not in str(caught.value)
