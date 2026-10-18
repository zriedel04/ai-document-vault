"""The DOCX extractor, on synthetic documents built here with python-docx and on hand-built zips."""

import io
import struct
import tracemalloc
import zipfile
import zlib
from collections.abc import Callable
from typing import NoReturn

import docx
import pytest
from docx.document import Document as DocumentObject
from docx.opc.constants import CONTENT_TYPE, RELATIONSHIP_TYPE
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from vault.ingest.extractors import Extraction, ExtractionError
from vault.ingest.extractors import docx as extractor
from vault.ingest.extractors.docx import (
    CELL_SEPARATOR,
    PARAGRAPH_BREAK,
    VERSION,
    NotDocxError,
    extract,
)
from vault.ingest.validate import OLE_SIGNATURE, Limits, Reason, check_docx

# Letters outside ASCII, from code points: e acute, an em dash, the CJK word for resume, and a
# character outside the Basic Multilingual Plane (one Python character, two UTF-16 units).
WORDS = "café — 履歴書 \U0002000b"
CANARY = "canary-secret-xyzzy"
W = nsdecls("w")
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
WPS = 'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
VML = 'xmlns:v="urn:schemas-microsoft-com:vml"'


def build(fill: Callable[[DocumentObject], object] | None = None) -> bytes:
    document = docx.Document()
    if fill is not None:
        fill(document)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def text_of(data: bytes) -> str:
    result = extract(data)
    assert len(result.pages) == 1
    return result.pages[0].text


def raw_zip(members: list[tuple[str, bytes, int, int, int]]) -> bytes:
    """A zip written by hand: (name, stored bytes, method, CRC, claimed size) per member, so the
    claims can differ from the data."""
    body, directory = b"", b""
    for name, stored, method, crc, size in members:
        encoded = name.encode()
        offset = len(body)
        header = struct.pack(
            "<4s5H3L2H",
            b"PK\x03\x04",
            20,
            0,
            method,
            0,
            33,
            crc,
            len(stored),
            size,
            len(encoded),
            0,
        )
        body += header + encoded + stored
        directory += (
            struct.pack(
                "<4s6H3L5H2L",
                b"PK\x01\x02",
                20,
                20,
                0,
                method,
                0,
                33,
                crc,
                len(stored),
                size,
                len(encoded),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
            + encoded
        )
    count = len(members)
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, count, count, len(directory), len(body), 0)
    return body + directory + end


def members_of(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(info.filename, archive.read(info)) for info in archive.infolist()]


def deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def replace_member(data: bytes, name: str, content: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, original in members_of(data):
            archive.writestr(member, content if member == name else original)
    return out.getvalue()


def add_xml(paragraph_owner: DocumentObject, xml: str) -> None:
    """Append a paragraph written as XML to the body, before the section properties."""
    body = paragraph_owner.element.body
    body.insert(len(body) - 1, parse_xml(xml))


@pytest.fixture
def library_never_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> NoReturn:
        raise AssertionError("python-docx was called")

    monkeypatch.setattr(extractor, "Document", refuse)
    monkeypatch.setattr(extractor, "_bounded_copy", refuse)


# The result


def test_paragraphs_in_order_one_page_and_the_version() -> None:
    def fill(document: DocumentObject) -> None:
        document.add_paragraph("First paragraph.")
        document.add_paragraph("")  # empty paragraphs are left out
        document.add_paragraph("   ")
        document.add_paragraph("Second paragraph.")

    result = extract(build(fill))

    assert isinstance(result, Extraction)
    assert result.extractor == VERSION == "docx-1"
    assert result.encoding is None
    assert [page.number for page in result.pages] == [1]
    assert result.pages[0].text == f"First paragraph.{PARAGRAPH_BREAK}Second paragraph."


def test_an_empty_document_is_one_empty_page() -> None:
    result = extract(build())

    assert result.pages[0].text == ""
    assert result.pages[0].headings == ()


def test_unicode_is_kept_and_offsets_count_characters() -> None:
    def fill(document: DocumentObject) -> None:
        document.add_paragraph(WORDS)
        document.add_heading(WORDS, level=2)

    page = extract(build(fill)).pages[0]

    assert page.text == f"{WORDS}{PARAGRAPH_BREAK}{WORDS}"
    (heading,) = page.headings
    assert page.text[heading.start : heading.end] == WORDS
    assert heading.start == len(WORDS) + len(PARAGRAPH_BREAK)


def test_line_breaks_inside_a_paragraph_are_newlines() -> None:
    def fill(document: DocumentObject) -> None:
        paragraph = document.add_paragraph("one")
        paragraph.add_run().add_break()
        paragraph.add_run("two\rthree")  # python-docx writes \r as a break as well
        add_xml(
            document, f"<w:p {W}><w:r><w:t>four</w:t><w:cr/><w:t>five&#13;six</w:t></w:r></w:p>"
        )

    assert text_of(build(fill)) == f"one\ntwo\nthree{PARAGRAPH_BREAK}four\nfive\nsix"


def test_tabs_and_special_characters_in_runs() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(
            document,
            f"<w:p {W}><w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:noBreakHyphen/>"
            "<w:t>c</w:t><w:softHyphen/><w:t>d</w:t></w:r></w:p>",
        )

    assert text_of(build(fill)) == "a\tb-cd"


# Headings


def test_headings_at_every_level_and_the_title() -> None:
    def fill(document: DocumentObject) -> None:
        document.add_heading("The title", level=0)
        for level in range(1, 10):
            document.add_heading(f"Level {level}", level=level)
            document.add_paragraph(f"Body under {level}.")

    page = extract(build(fill)).pages[0]

    assert [(h.level, h.title) for h in page.headings] == [
        (1, "The title"),
        *((level, f"Level {level}") for level in range(1, 10)),
    ]


def test_every_heading_offset_points_at_its_heading_text() -> None:
    def fill(document: DocumentObject) -> None:
        document.add_paragraph("Before any heading.")
        document.add_heading(f"Intro {WORDS}", level=1)
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "x"
        document.add_heading("  Spaced   title  ", level=2)
        heading = document.add_heading("Broken", level=3)
        heading.add_run().add_break()
        heading.add_run("over lines")
        document.add_paragraph("")
        document.add_heading("Last", level=1)

    page = extract(build(fill)).pages[0]

    assert [h.title for h in page.headings] == [
        f"Intro {WORDS}",
        "Spaced title",
        "Broken over lines",
        "Last",
    ]
    for heading in page.headings:
        written = page.text[heading.start : heading.end]
        assert " ".join(written.split()) == heading.title
        assert heading.start == 0 or page.text[heading.start - 2 : heading.start] == "\n\n"
        assert heading.end == len(page.text) or page.text[heading.end : heading.end + 2] == "\n\n"
    assert [h.start for h in page.headings] == sorted(h.start for h in page.headings)


def test_only_the_built_in_heading_styles_are_headings() -> None:
    def fill(document: DocumentObject) -> None:
        document.add_paragraph("Subtitle text", style="Subtitle")
        document.add_paragraph("Looks like a heading", style="Intense Quote")
        run_styled = document.add_paragraph().add_run("character style")
        run_styled.style = document.styles["Heading 1 Char"]
        add_xml(
            document,
            f'<w:p {W}><w:pPr><w:pStyle w:val="NoSuchStyle"/></w:pPr>'
            "<w:r><w:t>unknown style</w:t></w:r></w:p>",
        )

    assert extract(build(fill)).pages[0].headings == ()


def test_a_heading_style_is_found_by_name_not_by_id() -> None:
    # A Word in another language writes another style id for the same built-in name.
    def fill(document: DocumentObject) -> None:
        document.add_heading("Kapitel", level=2)

    data = build(fill)
    styles = dict(members_of(data))["word/styles.xml"]
    document_xml = dict(members_of(data))["word/document.xml"]
    renamed = replace_member(
        data, "word/styles.xml", styles.replace(b'"Heading2"', b'"berschrift2"')
    )
    renamed = replace_member(
        renamed, "word/document.xml", document_xml.replace(b'"Heading2"', b'"berschrift2"')
    )

    assert [(h.level, h.title) for h in extract(renamed).pages[0].headings] == [(2, "Kapitel")]


def test_a_style_that_is_not_a_paragraph_style_is_not_a_heading() -> None:
    def fill(document: DocumentObject) -> None:
        document.add_heading("Not a section", level=2)

    data = build(fill)
    styles = dict(members_of(data))["word/styles.xml"]
    old = b'<w:style w:type="paragraph" w:styleId="Heading2">'
    assert old in styles
    changed = styles.replace(old, b'<w:style w:type="character" w:styleId="Heading2">')

    assert extract(replace_member(data, "word/styles.xml", changed)).pages[0].headings == ()


def test_a_heading_inside_a_table_or_a_text_box_is_not_a_section_heading() -> None:
    def fill(document: DocumentObject) -> None:
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).paragraphs[0].text = "In a cell"
        table.cell(0, 0).paragraphs[0].style = document.styles["Heading 1"]
        add_xml(document, text_box_paragraph("Boxed heading", style="Heading1"))

    page = extract(build(fill)).pages[0]

    assert "In a cell" in page.text
    assert "Boxed heading" in page.text
    assert page.headings == ()


# Tables


def test_a_table_is_one_line_per_row_with_tab_separated_cells() -> None:
    def fill(document: DocumentObject) -> None:
        document.add_paragraph("Before.")
        table = document.add_table(rows=3, cols=3)
        for row, values in enumerate([("Name", "Hours", "Note"), ("Alex", "41", ""), ("", "", "")]):
            for column, value in enumerate(values):
                table.cell(row, column).text = value
        document.add_paragraph("After.")

    assert CELL_SEPARATOR == "\t"
    assert text_of(build(fill)) == (
        f"Before.{PARAGRAPH_BREAK}Name\tHours\tNote\nAlex\t41\t{PARAGRAPH_BREAK}After."
    )


def test_cell_paragraphs_breaks_tabs_and_nested_tables_stay_on_the_row() -> None:
    def fill(document: DocumentObject) -> None:
        table = document.add_table(rows=1, cols=2)
        cell = table.cell(0, 0)
        cell.text = "first"
        cell.add_paragraph("second\tpart")
        cell.paragraphs[1].add_run().add_break()
        cell.paragraphs[1].add_run("third")
        inner = table.cell(0, 1).add_table(rows=2, cols=2)
        for row in range(2):
            for column in range(2):
                inner.cell(row, column).text = f"r{row}c{column}"

    assert text_of(build(fill)) == "first second part third\tr0c0 r0c1 r1c0 r1c1"


def test_a_merged_cell_appears_once() -> None:
    def fill(document: DocumentObject) -> None:
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).merge(table.cell(0, 1)).text = "wide"
        table.cell(1, 0).text = "a"
        table.cell(1, 1).text = "b"

    assert text_of(build(fill)) == "wide\na\tb"


# Tracked changes


def test_tracked_changes_are_read_as_accepted() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(
            document,
            f'<w:p {W}><w:r><w:t xml:space="preserve">Keep </w:t></w:r>'
            '<w:ins w:id="1" w:author="A"><w:r><w:t>inserted</w:t></w:r></w:ins>'
            # A deleted run keeps its tabs and breaks as w:tab and w:br, not as deleted text.
            f'<w:del w:id="2" w:author="A"><w:r><w:delText>{CANARY}</w:delText><w:tab/><w:br/>'
            "</w:r></w:del>"
            f'<w:moveFrom w:id="3" w:author="A"><w:r><w:t>{CANARY}-moved</w:t></w:r></w:moveFrom>'
            '<w:moveTo w:id="4" w:author="A"><w:r><w:t xml:space="preserve"> moved in</w:t>'
            "</w:r></w:moveTo></w:p>",
        )

    text = text_of(build(fill))

    assert text == "Keep inserted moved in"
    assert CANARY not in text


def test_a_row_deleted_as_a_tracked_change_is_left_out() -> None:
    def fill(document: DocumentObject) -> None:
        table = document.add_table(rows=2, cols=1)
        table.cell(0, 0).text = "kept row"
        table.cell(1, 0).text = CANARY
        row = table.rows[1]._tr
        row.insert(0, parse_xml(f'<w:trPr {W}><w:del w:id="9" w:author="A"/></w:trPr>'))

    assert text_of(build(fill)) == "kept row"


# What is left out


def test_hidden_text_is_left_out() -> None:
    def fill(document: DocumentObject) -> None:
        paragraph = document.add_paragraph("Visible ")
        paragraph.add_run(CANARY).font.hidden = True
        paragraph.add_run("text")
        add_xml(
            document,
            f'<w:p {W}><w:r><w:rPr><w:vanish w:val="false"/></w:rPr>'
            "<w:t>shown again</w:t></w:r></w:p>",
        )

    assert text_of(build(fill)) == f"Visible text{PARAGRAPH_BREAK}shown again"


def test_headers_and_footers_are_left_out() -> None:
    def fill(document: DocumentObject) -> None:
        document.sections[0].header.paragraphs[0].text = f"{CANARY} header"
        document.sections[0].footer.paragraphs[0].text = f"{CANARY} footer"
        document.add_paragraph("Body.")

    assert text_of(build(fill)) == "Body."


def test_comments_are_left_out() -> None:
    def fill(document: DocumentObject) -> None:
        paragraph = document.add_paragraph("Commented text.")
        document.add_comment(paragraph.runs, text=f"{CANARY} comment", author="A")

    data = build(fill)

    assert "word/comments.xml" in dict(members_of(data))
    assert text_of(data) == "Commented text."


def test_footnotes_are_left_out() -> None:
    def fill(document: DocumentObject) -> None:
        notes = (
            f'<w:footnotes {W}><w:footnote w:id="1"><w:p><w:r><w:t>{CANARY} note</w:t></w:r>'
            "</w:p></w:footnote></w:footnotes>"
        ).encode()
        part = Part(
            PackURI("/word/footnotes.xml"),
            CONTENT_TYPE.WML_FOOTNOTES,
            notes,
            document.part.package,
        )
        document.part.relate_to(part, RELATIONSHIP_TYPE.FOOTNOTES)
        add_xml(
            document,
            f'<w:p {W}><w:r><w:t>Claim</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>',
        )

    data = build(fill)

    assert "word/footnotes.xml" in dict(members_of(data))
    assert text_of(data) == "Claim"


def test_field_codes_are_left_out_and_results_and_links_kept() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(
            document,
            f'<w:p {W} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r><w:instrText xml:space="preserve"> HYPERLINK "{CANARY}" </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>field result</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            '<w:r><w:t xml:space="preserve">, </w:t></w:r>'
            '<w:hyperlink r:id="rIdMissing"><w:r><w:t>link text</w:t></w:r></w:hyperlink>'
            '<w:fldSimple w:instr=" PAGE "><w:r><w:t>, 7</w:t></w:r></w:fldSimple></w:p>',
        )

    assert text_of(build(fill)) == "field result, link text, 7"


def test_content_controls_are_read_through() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(
            document,
            f'<w:sdt {W}><w:sdtPr><w:alias w:val="{CANARY}"/></w:sdtPr><w:sdtContent>'
            "<w:p><w:r><w:t>block control</w:t></w:r>"
            '<w:sdt><w:sdtContent><w:r><w:t xml:space="preserve"> and inline</w:t></w:r>'
            "</w:sdtContent></w:sdt></w:p></w:sdtContent></w:sdt>",
        )

    assert text_of(build(fill)) == "block control and inline"


def test_custom_xml_wrappers_are_read_through() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(
            document,
            f'<w:customXml {W} w:element="clause"><w:p><w:r><w:t>wrapped</w:t></w:r></w:p>'
            "</w:customXml>",
        )

    assert text_of(build(fill)) == "wrapped"


def test_alternate_content_in_a_paragraph_is_read_from_the_first_choice_only() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(
            document,
            f'<w:p {W} {MC}><mc:AlternateContent><mc:Choice Requires="w14">'
            f"<w:r><w:t>new form</w:t></w:r></mc:Choice><mc:Fallback><w:r><w:t>{CANARY}</w:t>"
            "</w:r></mc:Fallback></mc:AlternateContent></w:p>",
        )

    assert text_of(build(fill)) == "new form"


# Text boxes


def text_box_paragraph(text: str, *, style: str | None = None) -> str:
    """A paragraph anchoring a text box, written the way Word does: a DrawingML shape in
    ``mc:Choice`` and the same box again as VML in ``mc:Fallback``."""
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    box = f"<w:txbxContent><w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent>"
    return (
        f'<w:p {W} {MC} {WPS} {VML}><w:r><w:t xml:space="preserve">Anchor </w:t></w:r><w:r>'
        '<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx>'
        f"{box}</wps:txbx></wps:wsp></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape>"
        f"<v:textbox>{box}</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent>"
        "</w:r><w:r><w:t>text.</w:t></w:r></w:p>"
    )


def test_a_text_box_is_read_once_after_its_anchor() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(document, text_box_paragraph("Boxed note"))
        document.add_paragraph("Next.")

    assert text_of(build(fill)) == (
        f"Anchor text.{PARAGRAPH_BREAK}Boxed note{PARAGRAPH_BREAK}Next."
    )


def test_a_vml_only_text_box_is_read() -> None:
    def fill(document: DocumentObject) -> None:
        add_xml(
            document,
            f"<w:p {W} {VML}><w:r><w:pict><v:shape><v:textbox><w:txbxContent><w:p><w:r>"
            "<w:t>Old box</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict>"
            "</w:r></w:p>",
        )

    assert text_of(build(fill)) == "Old box"


# Refused input


def test_errors_are_extraction_errors_with_fixed_text() -> None:
    assert issubclass(NotDocxError, ExtractionError)
    assert str(NotDocxError(Reason.UNSAFE_ARCHIVE)) == (
        "The file is not a readable DOCX (unsafe_archive)."
    )
    assert str(NotDocxError()) == "The file is not a readable DOCX (unreadable_part)."


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", Reason.EMPTY),
        (OLE_SIGNATURE + b"\x00" * 504, Reason.LEGACY_OR_ENCRYPTED_OFFICE),
        (b"PK\x03\x04" + bytes(range(200)), Reason.UNREADABLE_ARCHIVE),
        (f"{CANARY} plain text".encode(), Reason.CONTENT_MISMATCH),
        (b"%PDF-1.7\n%%EOF\n", Reason.CONTENT_MISMATCH),
    ],
)
def test_bytes_that_are_not_a_docx(data: bytes, reason: Reason, library_never_called: None) -> None:
    with pytest.raises(NotDocxError) as raised:
        extract(data)

    assert raised.value.reason is reason
    assert CANARY not in str(raised.value)


def test_a_zip_that_is_not_a_docx(library_never_called: None) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("hello.txt", CANARY)

    with pytest.raises(NotDocxError) as raised:
        extract(out.getvalue())

    assert raised.value.reason is Reason.CONTENT_MISMATCH


def test_a_docx_whose_xml_is_broken_gives_fixed_text_without_the_library_error() -> None:
    data = replace_member(build(), "word/document.xml", f"<w:document>{CANARY}".encode())

    with pytest.raises(NotDocxError) as raised:
        extract(data)

    assert raised.value.reason is None
    assert CANARY not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_a_package_whose_main_part_is_not_a_word_document_is_refused() -> None:
    data = build()
    types = dict(members_of(data))["[Content_Types].xml"]
    macro = types.replace(
        b"wordprocessingml.document.main+xml", b"ms-word.document.macroEnabled.main+xml"
    )
    assert macro != types

    with pytest.raises(NotDocxError):
        extract(replace_member(data, "[Content_Types].xml", macro))


def test_a_strict_open_xml_document_is_refused_not_read_as_empty() -> None:
    data = build(lambda d: d.add_paragraph("Strict text."))
    document_xml = dict(members_of(data))["word/document.xml"]
    strict = document_xml.replace(
        b"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        b"http://purl.oclc.org/ooxml/wordprocessingml/main",
    )
    assert strict != document_xml

    with pytest.raises(NotDocxError) as raised:
        extract(replace_member(data, "word/document.xml", strict))

    assert raised.value.reason is None


def test_a_member_whose_crc_does_not_match_is_unreadable() -> None:
    members = [
        (name, deflate(content), zipfile.ZIP_DEFLATED, zlib.crc32(content) ^ 1, len(content))
        for name, content in members_of(build())
    ]

    with pytest.raises(NotDocxError) as raised:
        extract(raw_zip(members))

    assert raised.value.reason is Reason.UNREADABLE_ARCHIVE


def test_folder_entries_in_the_zip_are_skipped() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/", b"")
        for name, content in members_of(build(lambda d: d.add_paragraph("With folders."))):
            archive.writestr(name, content)

    assert text_of(out.getvalue()) == "With folders."


def test_a_member_is_never_read_past_its_claim_even_if_zipfile_did() -> None:
    class Archive:
        def open(self, _: zipfile.ZipInfo) -> io.BytesIO:
            return io.BytesIO(b"x" * 10)

    info = zipfile.ZipInfo("word/document.xml")
    info.file_size = 4

    with pytest.raises(NotDocxError) as raised:
        extractor._read_member(Archive(), info)  # type: ignore[arg-type]

    assert raised.value.reason is Reason.UNSAFE_ARCHIVE


def test_running_out_of_memory_is_not_reported_as_a_bad_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exhausted(*_: object) -> NoReturn:
        raise MemoryError

    monkeypatch.setattr(extractor, "Document", exhausted)

    with pytest.raises(MemoryError):
        extract(build())


def test_check_docx_agrees_with_the_import_check() -> None:
    assert check_docx(build()) is None
    assert check_docx(b"x" * 11, Limits(max_bytes=10)) is Reason.TOO_LARGE


# Zip bombs


def test_a_compression_bomb_is_refused_before_python_docx_opens_it(
    library_never_called: None,
) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members_of(build()):
            archive.writestr(name, content)
        archive.writestr("word/media/zeros.bin", b"\x00" * 20_000_000)
    assert len(out.getvalue()) < 100_000

    with pytest.raises(NotDocxError) as raised:
        extract(out.getvalue())

    assert raised.value.reason is Reason.UNSAFE_ARCHIVE


def test_a_directory_over_the_limits_is_refused_before_zipfile_opens_it(
    library_never_called: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> NoReturn:
        raise AssertionError("zipfile was opened")

    monkeypatch.setattr("vault.ingest.validate.zipfile.ZipFile", refuse)

    with pytest.raises(NotDocxError) as raised:
        extract(build(), Limits(max_zip_entries=3))

    assert raised.value.reason is Reason.UNSAFE_ARCHIVE


def test_a_member_longer_than_its_claim_is_never_decompressed_past_the_claim() -> None:
    """zipfile's ``read`` decompresses a whole deflate stream before cutting it to the claimed size,
    so a stream far longer than its claim would fill memory inside python-docx. The extractor reads
    members in pieces and hands python-docx a copy that holds only the claimed bytes."""

    def fill(document: DocumentObject) -> None:
        document.add_paragraph("The real text.")

    padding = 64 * 1024 * 1024
    members = []
    for name, content in members_of(build(fill)):
        stream = content + b" " * padding if name == "word/document.xml" else content
        # The CRC and size of the claimed prefix only, so the claim checks out where it stops.
        members.append(
            (name, deflate(stream), zipfile.ZIP_DEFLATED, zlib.crc32(content), len(content))
        )
    data = raw_zip(members)
    assert check_docx(data) is None  # the claims are small, so validation passes

    # Control: reading the member the way python-docx does allocates the whole stream.
    tracemalloc.start()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.read("word/document.xml")
    _, unbounded = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert unbounded > padding

    tracemalloc.start()
    try:
        result = extract(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.pages[0].text == "The real text."
    assert peak < padding // 4
