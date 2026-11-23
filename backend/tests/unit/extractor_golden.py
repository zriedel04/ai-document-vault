"""The extractor fixture set and its golden files (plan, section 13, Phase 2 testing).

Every fixture is built here, in code, from text written as code points, so the set holds no binary
file and is the same on every system: the PDFs with ``pdf_builder``, the DOCX files as uncompressed
zips written member by member (fixed dates and attributes, so the bytes do not depend on the system,
python-docx's template or zlib), and the text files as encoded strings. Each golden file in
``GOLDEN_DIR`` records one fixture's SHA-256 and what the product's extractor gives for it: its
pages, text, headings and offsets (and for a PDF page its measurements and the OCR decision the
extraction stage stores for it), or the class of the error that refused it (and the validator's
reason, when the error carries one).

The extractors run the way the extraction stage runs them: through the sandbox entry points
(``vault.jobs.sandbox.ENTRY_POINTS``), in a child process with the stage's limits, the text
extractors with the fallback code page as their option. Only an error's class name comes back
from the sandbox, so each fixture is also extracted in this process, which must give the same
result, and gives the reason the golden file records.

What a golden file records is what the current extractor versions (``text-1``, ``docx-1``,
``pdf-2``) give, not what they should give: the PDF fixtures in particular record pdf-2's reading
order across two columns, which ADR 0015 (Proposed) leaves to the owner's PDF spike.

To regenerate the golden files after a deliberate change to a fixture or an extractor, run in
``backend/``::

    uv run python -m tests.unit.extractor_golden

and review the diff of ``tests/fixtures/extractors/`` before committing it.
``tests/unit/test_extractor_golden.py`` fails when an extractor's output drifts from them.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vault.db.pages import PageText
from vault.ingest import extract_stage
from vault.ingest.extractors import Extraction, ExtractionError, Page
from vault.ingest.extractors import docx as docx_extractor
from vault.ingest.extractors import pdf as pdf_extractor
from vault.ingest.extractors import text as text_extractor
from vault.ingest.validate import OLE_SIGNATURE
from vault.jobs import sandbox

from . import pdf_builder

GOLDEN_DIR: Final = Path(__file__).resolve().parent.parent / "fixtures" / "extractors"
LIMITS: Final = sandbox.SandboxLimits(timeout_seconds=60, memory_bytes=512 * sandbox.MIB)
REGENERATE: Final = "uv run python -m tests.unit.extractor_golden"

# The same extractors as the sandbox's entry points, called in this process.
DIRECT: Final[dict[str, Callable[..., Extraction]]] = {
    "text": text_extractor.extract_plain,
    "markdown": text_extractor.extract_markdown,
    "docx": docx_extractor.extract,
    "pdf": pdf_extractor.extract,
}

# Non-English words, from code points so that this file is ASCII.
HEBREW: Final = "שלום עולם"  # shalom olam
ARABIC: Final = "مرحبا بالعالم"
CHINESE: Final = "文档库"  # document library
JAPANESE: Final = "履歴書と職務経歴書"  # resume and CV
KOREAN: Final = "한국어 문서"  # Korean document
# Combining marks, decomposed: e and an acute accent, Vietnamese with two marks on one letter.
COMBINING: Final = "Café and Tiếng Việt"
CYRILLIC: Final = "Дневник курса"
WESTERN: Final = "crème brûlée, naïve façade"
OUTSIDE_BMP: Final = "\U0002000b"  # a CJK ideograph outside the Basic Multilingual Plane


@dataclass(frozen=True)
class Fixture:
    """One input: its name (the golden file's stem), the file type the extraction stage would
    see (a key of ``extract_stage.EXTRACTORS``), what it covers, how to build it, and the fallback
    code page configured for text (``[processing] text_fallback_encoding``)."""

    name: str
    ext: str
    about: str
    build: Callable[[], bytes]
    fallback: str = text_extractor.FALLBACK_ENCODING

    @property
    def extractor(self) -> extract_stage.Extractor:
        return extract_stage.EXTRACTORS[self.ext]

    @property
    def entry(self) -> str:
        return self.extractor.entry

    @property
    def option(self) -> str | None:
        """The option the stage passes to the entry point: the fallback, for text only."""
        return self.fallback if self.extractor.text else None


# --- Text and Markdown ---------------------------------------------------------------------------

MULTILINGUAL_LINES: Final = (
    "Notes in several scripts",
    f"Hebrew (right to left): {HEBREW}",
    f"Arabic (right to left): {ARABIC}",
    f"Chinese: {CHINESE}, Japanese: {JAPANESE}, Korean: {KOREAN}",
    f"Combining marks: {COMBINING}",
    f"Outside the BMP: {OUTSIDE_BMP}",
)


def _multilingual_text() -> str:
    return "\r\n".join(MULTILINGUAL_LINES) + "\r\n"


MARKDOWN_HEADINGS: Final = "\n".join(
    [
        "---",
        "title: Front matter is skipped",
        "---",
        "# Field guide",
        "",
        "Opening paragraph.",
        "",
        f"## {HEBREW}",
        "",
        f"{ARABIC}",
        "",
        f"{JAPANESE}",
        "=========",
        "",
        "Text under a setext heading.",
        "",
        f"### {COMBINING} ###",
        "",
        "```",
        "# not a heading inside a code fence",
        "```",
        "",
        "> # not a section heading inside a block quote",
        "",
        "- # nor inside a list item",
        "",
        "    # nor in an indented code block",
        "",
        "Setext with a lazy",
        "second line",
        "---",
        "",
        "###### Level six",
        "",
    ]
)

MARKDOWN_TABLE: Final = "\n".join(
    [
        "# Grades",
        "",
        "| Course | Credits | Grade |",
        "| --- | ---: | :-: |",
        "| CSE 485 | 3 | A |",
        f"| {CHINESE} | 2 | B |",
        f"| {HEBREW} | 1 | |",
        "",
        "## After the table",
        "",
        "Closing line.",
        "",
    ]
)


def _cut(data: bytes, keep: int) -> bytes:
    """The first ``keep`` bytes, checked to be a real cut."""
    if not 0 < keep < len(data):
        msg = "a cut keeps part of the data"
        raise ValueError(msg)
    return data[:keep]


def _utf8_cut_mid_character() -> bytes:
    data = _multilingual_text().encode("utf-8")
    cut = data.index(CHINESE.encode("utf-8")) + 1  # one byte into a three-byte character
    return _cut(data, cut)


def _western_utf8_cut_mid_character() -> bytes:
    data = (WESTERN + "\n").encode("utf-8")
    # One byte into the last two-byte character, so every letter before it is valid UTF-8.
    return _cut(data, data.rindex("\u00e7".encode()) + 1)


def _utf16_odd_length() -> bytes:
    data = b"\xff\xfe" + _multilingual_text().encode("utf-16-le")
    return _cut(data, len(data) - 1)


def _text_turns_binary() -> bytes:
    head = ("Readable head of the file.\n" * 400).encode("ascii")
    return head + bytes(range(256)) * 4


def _markdown_cut_in_table() -> bytes:
    data = MARKDOWN_TABLE.encode("utf-8")
    return _cut(data, data.index(b"| CSE 485") + 5)


def _markdown_bad_utf8_after_mark() -> bytes:
    return b"\xef\xbb\xbf# Heading\n\nText \xff\xfe broken\n"


# --- DOCX ----------------------------------------------------------------------------------------

W_NS: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_DATE: Final = (2026, 1, 1, 0, 0, 0)

CONTENT_TYPES: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
PACKAGE_RELS: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
DOCUMENT_RELS: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)
# Style ids are not names: the extractor matches the name, so the ids here differ from them.
_STYLES: Final = (
    ("Normal", "Normal", None),
    ("TitleStyle", "Title", "Normal"),
    ("H1", "heading 1", "Normal"),
    ("H2", "heading 2", "Normal"),
    ("H3", "heading 3", "Normal"),
    ("H9", "heading 9", "Normal"),
    # Looks like a heading and is based on one, but is not a built-in heading style.
    ("Lookalike", "Heading One Look", "H1"),
)


def _styles_xml() -> str:
    styles = "".join(
        f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
        + (f'<w:basedOn w:val="{based}"/>' if based else "")
        + "</w:style>"
        for style_id, name, based in _STYLES
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:styles xmlns:w="{W_NS}">{styles}</w:styles>'
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _run(text: str, *, rtl: bool = False) -> str:
    properties = "<w:rPr><w:rtl/></w:rPr>" if rtl else ""
    return f'<w:r>{properties}<w:t xml:space="preserve">{_escape(text)}</w:t></w:r>'


def _paragraph(content: str | list[str], style: str | None = None, *, rtl: bool = False) -> str:
    """A paragraph of runs: plain text, or run XML when ``content`` is a list."""
    runs = "".join(content) if isinstance(content, list) else _run(content, rtl=rtl)
    properties = ""
    if style is not None or rtl:
        style_xml = f'<w:pStyle w:val="{style}"/>' if style is not None else ""
        properties = f"<w:pPr>{style_xml}{'<w:bidi/>' if rtl else ''}</w:pPr>"
    return f"<w:p>{properties}{runs}</w:p>"


def _cell(*paragraphs: str, span: int = 1) -> str:
    properties = f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>' if span > 1 else ""
    return f"<w:tc>{properties}{''.join(paragraphs) or '<w:p/>'}</w:tc>"


def _table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join(f"<w:tr>{''.join(row)}</w:tr>" for row in rows) + "</w:tbl>"


def _document_xml(body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )


def stored_zip(members: list[tuple[str, bytes]]) -> bytes:
    """An uncompressed zip of ``members`` whose bytes depend on nothing but the members: fixed
    dates, attributes and creating system (``zipfile`` writes 0 on Windows and 3 elsewhere)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=_DOCX_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return out.getvalue()


def docx_package(body: str, *, document: bytes | None = None) -> bytes:
    """A DOCX with ``body`` in its main part (or ``document`` as the main part's bytes)."""
    main = _document_xml(body).encode("utf-8") if document is None else document
    return stored_zip(
        [
            ("[Content_Types].xml", CONTENT_TYPES.encode("utf-8")),
            ("_rels/.rels", PACKAGE_RELS.encode("utf-8")),
            ("word/document.xml", main),
            ("word/_rels/document.xml.rels", DOCUMENT_RELS.encode("utf-8")),
            ("word/styles.xml", _styles_xml().encode("utf-8")),
        ]
    )


def _docx_headings() -> bytes:
    break_and_tab = [
        _run("A line"),
        "<w:r><w:br/></w:r>",
        _run("break and a"),
        "<w:r><w:tab/></w:r>",
        _run("tab."),
    ]
    body = "".join(
        [
            _paragraph("Field Guide", "TitleStyle"),
            _paragraph("Introduction", "H1"),
            _paragraph(break_and_tab),
            _paragraph(""),  # an empty paragraph is left out
            _paragraph(HEBREW, "H2", rtl=True),
            _paragraph(ARABIC, rtl=True),
            _paragraph(f"{CHINESE} {JAPANESE}", "H2"),
            _paragraph(f"{KOREAN} {OUTSIDE_BMP}"),
            _paragraph(COMBINING, "H3"),
            _paragraph("  Spaced   heading  title  ", "H3"),
            _paragraph("Styled like a heading, but not one", "Lookalike"),
            _table([[_cell(_paragraph("A heading style in a table cell", "H1"))]]),
            _paragraph("Deepest level", "H9"),
            _paragraph("Closing paragraph."),
        ]
    )
    return docx_package(body)


def _docx_tables() -> bytes:
    nested = _table([[_cell(_paragraph("inner a")), _cell(_paragraph("inner b"))]])
    grades = _table(
        [
            [_cell(_paragraph(h)) for h in ("Course", "Credits", "Grade")],
            [_cell(_paragraph("CSE 485")), _cell(_paragraph("3")), _cell(_paragraph("A"))],
            [_cell(_paragraph(CHINESE)), _cell(_paragraph("2")), _cell(_paragraph("B"))],
            [_cell(_paragraph(HEBREW, rtl=True)), _cell(_paragraph("1")), _cell()],
            [_cell(), _cell(), _cell()],  # an empty row is left out
            [_cell(_paragraph("Spans two columns"), span=2), _cell(_paragraph("C"))],
            [
                _cell(_paragraph("Two"), _paragraph("paragraphs")),
                _cell(_paragraph(["<w:r><w:t>tab</w:t></w:r>", "<w:r><w:tab/></w:r>", _run("in")])),
                _cell(nested, _paragraph("after nested")),
            ],
        ]
    )
    body = "".join(
        [
            _paragraph("Grades", "H1"),
            grades,
            _paragraph("After the table", "H2"),
            _paragraph("Closing line."),
        ]
    )
    return docx_package(body)


def _ole_container() -> bytes:
    """The signature of an OLE compound file, the container of a password-protected (or legacy)
    Word file, and filler: the validator's DOCX rule refuses it by its signature."""
    return OLE_SIGNATURE + bytes(504)


def _docx_malformed_xml() -> bytes:
    good = _document_xml(_paragraph("Unclosed")).encode("utf-8")
    return docx_package("", document=good.replace(b"</w:body>", b""))


def _docx_bad_crc() -> bytes:
    data = bytearray(_docx_headings())
    position = data.index(b"Introduction")
    data[position] ^= 0x20  # "introduction": the stored bytes no longer match their CRC
    return bytes(data)


# --- PDF -----------------------------------------------------------------------------------------

LEFT_COLUMN: Final = (
    "Field notes on the river",
    "survey began in early",
    "spring, when the water",
    "was high.",
)
RIGHT_COLUMN: Final = (
    "The second column lists",
    "the stations sampled",
    "each week and the",
    "readings taken there.",
)


def _shown(x: int, y: int, line: str, size: int = 11) -> bytes:
    escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return (
        f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm (".encode() + escaped.encode("latin-1") + b") Tj ET"
    )


def _two_columns(*, by_row: bool) -> bytes:
    """Two columns on one page, side by side on the same baselines. ``by_row`` writes the content
    stream a row at a time (left line, right line, ...), else a column at a time."""
    left = [_shown(72, 720 - 14 * i, line) for i, line in enumerate(LEFT_COLUMN)]
    right = [_shown(320, 720 - 14 * i, line) for i, line in enumerate(RIGHT_COLUMN)]
    if by_row:
        shown = [part for pair in zip(left, right, strict=True) for part in pair]
    else:
        shown = left + right
    return pdf_builder.build([b"\n".join([_shown(72, 750, "Two column page", 14), *shown])])


def _pdf_pages() -> bytes:
    return pdf_builder.build(
        [
            pdf_builder.text_stream(["First page", "The measure-", "ments were taken."]),
            None,  # a page with no content: no text layer, the mark for OCR
            pdf_builder.text_stream(["   "]),  # only white space: stored empty too
            pdf_builder.text_stream(["Fourth page (with parentheses) and a back\\slash"]),
        ]
    )


# One-byte codes the ToUnicode map gives non-Latin characters (and two code points for one glyph).
_UNICODE_CODES: Final = {
    0x80: "05E9",  # Hebrew shin
    0x81: "05DC",  # lamed
    0x82: "05D5",  # vav
    0x83: "05DD",  # final mem
    0x84: "6587",  # CJK: wen
    0x85: "6863",  # dang
    0x86: "5E93",  # ku
    0x87: "00650301",  # e followed by a combining acute accent, from one glyph
    0x88: "0301",  # a combining acute accent as a glyph of its own
    0x89: "D840DC0B",  # a character outside the BMP (a surrogate pair)
}


def _pdf_non_english() -> bytes:
    lines = [
        "Hebrew in logical order: \x80\x81\x82\x83",
        "Hebrew in visual order: \x83\x82\x81\x80",
        "CJK: \x84\x85\x86 \x89",
        "Combining: Caf\x87 and cafe\x88",
    ]
    return pdf_builder.build(
        [pdf_builder.text_stream(lines)], to_unicode=pdf_builder.to_unicode_map(_UNICODE_CODES)
    )


def _pdf_text_layers() -> bytes:
    sentence = "The survey began in early spring, when the water was high."
    return pdf_builder.build(
        [
            pdf_builder.text_stream([sentence, sentence]),
            pdf_builder.text_stream(["ABCDEFGHIJ", "ABCDEFGHIJ"]),
            pdf_builder.text_stream(["12"]),
            None,
            pdf_builder.text_stream(["A short label for a drawing"]),
            pdf_builder.text_stream(["A short label for a drawing"]),
        ],
        unknown_glyphs=range(0x41, 0x4B),
        sizes=[(612, 792)] * 4 + [(595, 842), (5000, 5000)],
    )


def _pdf_encrypted() -> bytes:
    return pdf_builder.build([pdf_builder.text_stream(["Secret"])], user_password=b"user")


def _pdf_owner_password_only() -> bytes:
    return pdf_builder.build(
        [pdf_builder.text_stream(["Restricted, not secret"])], user_password=b""
    )


def _pdf_truncated() -> bytes:
    data = _two_columns(by_row=False)
    return _cut(data, len(data) // 2)


def _pdf_cross_reference_cut() -> bytes:
    data = _two_columns(by_row=False)
    # The table starts a line of its own; a bare b"xref" also matches inside "startxref".
    cut = _cut(data, data.rindex(b"\nxref\n") + 1)
    if b"xref" in cut or b"trailer" in cut:
        msg = "the cut keeps part of the cross-reference table"
        raise ValueError(msg)
    return cut


def _pdf_garbage_body() -> bytes:
    return b"%PDF-1.4\n" + bytes(range(256)) * 8


def _pdf_page_tree_overclaims() -> bytes:
    return pdf_builder.build([pdf_builder.text_stream(["Only page"])], claimed_pages=3)


def _not_a_pdf() -> bytes:
    return b"This is a text file named as a PDF.\n"


FIXTURES: Final[tuple[Fixture, ...]] = (
    Fixture(
        "txt-utf8-multilingual",
        "txt",
        "UTF-8 without a mark: right-to-left, CJK, combining marks, a character outside the BMP, "
        "Windows line breaks",
        lambda: _multilingual_text().encode("utf-8"),
    ),
    Fixture(
        "txt-utf16le-bom",
        "txt",
        "The same text in UTF-16 LE with a byte order mark",
        lambda: b"\xff\xfe" + _multilingual_text().encode("utf-16-le"),
    ),
    Fixture(
        "txt-cp1251-fallback",
        "txt",
        "Cyrillic in Windows-1251 without a mark, with cp1251 as the fallback",
        lambda: (CYRILLIC + "\r\n").encode("cp1251"),
        "cp1251",
    ),
    Fixture(
        "txt-cp1252-fallback",
        "txt",
        "Western accents in Windows-1252 without a mark, with the default fallback",
        lambda: (WESTERN + "\n").encode("cp1252"),
    ),
    Fixture(
        "txt-utf8-cut-mid-character",
        "txt",
        "Multilingual UTF-8 cut in the middle of a character: not valid UTF-8, and not valid "
        "in the fallback code page either (bytes Windows-1252 leaves undefined)",
        _utf8_cut_mid_character,
    ),
    Fixture(
        "txt-utf8-western-cut-mid-character",
        "txt",
        "Western accents in UTF-8 cut in the middle of a character: not valid UTF-8, so the whole "
        "file is decoded with the fallback code page",
        _western_utf8_cut_mid_character,
    ),
    Fixture(
        "txt-empty",
        "txt",
        "A file of no bytes",
        lambda: b"",
    ),
    Fixture(
        "txt-utf16-odd-length",
        "txt",
        "UTF-16 with a mark, cut to an odd number of bytes",
        _utf16_odd_length,
    ),
    Fixture(
        "txt-turns-binary",
        "txt",
        "A readable head followed by binary bytes (NUL among them)",
        _text_turns_binary,
    ),
    Fixture(
        "md-headings",
        "md",
        "ATX and setext headings in several scripts, front matter, and heading lines that are not "
        "section headings (code, block quote, list item)",
        lambda: MARKDOWN_HEADINGS.encode("utf-8"),
    ),
    Fixture(
        "md-table",
        "md",
        "A pipe table (not part of CommonMark, kept as text) between headings",
        lambda: MARKDOWN_TABLE.encode("utf-8"),
    ),
    Fixture(
        "md-cut-in-table",
        "md",
        "The table file cut in the middle of a row",
        _markdown_cut_in_table,
    ),
    Fixture(
        "md-empty",
        "md",
        "A file of no bytes",
        lambda: b"",
    ),
    Fixture(
        "md-bad-utf8-after-mark",
        "md",
        "A UTF-8 byte order mark followed by bytes that are not UTF-8",
        _markdown_bad_utf8_after_mark,
    ),
    Fixture(
        "docx-headings",
        "docx",
        "Title and Heading 1, 2, 3 and 9 styles (matched by name), right-to-left and CJK "
        "headings, combining marks, a look-alike style and a heading style in a table cell",
        _docx_headings,
    ),
    Fixture(
        "docx-tables",
        "docx",
        "A table with a header row, a right-to-left cell, an empty row, a merged cell, a cell of "
        "two paragraphs, a tab in a cell and a nested table",
        _docx_tables,
    ),
    Fixture(
        "docx-truncated",
        "docx",
        "The headings DOCX cut in half (no central directory)",
        lambda: _cut(_docx_headings(), len(_docx_headings()) // 2),
    ),
    Fixture(
        "docx-ole-container",
        "docx",
        "An OLE compound file (how a password-protected or legacy Word file starts)",
        _ole_container,
    ),
    Fixture(
        "docx-empty",
        "docx",
        "A file of no bytes",
        lambda: b"",
    ),
    Fixture(
        "docx-malformed-xml",
        "docx",
        "A valid zip whose main part is not well-formed XML",
        _docx_malformed_xml,
    ),
    Fixture(
        "docx-bad-crc",
        "docx",
        "The headings DOCX with one stored byte changed, so its CRC no longer matches",
        _docx_bad_crc,
    ),
    Fixture(
        "pdf-two-columns-by-column",
        "pdf",
        "Two columns on one page, the content stream written a column at a time",
        lambda: _two_columns(by_row=False),
    ),
    Fixture(
        "pdf-two-columns-by-row",
        "pdf",
        "The same two columns, the content stream written a row at a time across both columns",
        lambda: _two_columns(by_row=True),
    ),
    Fixture(
        "pdf-pages",
        "pdf",
        "Four pages: a hyphenated word at a line end, a page with no content, a page of white "
        "space, and escaped characters",
        _pdf_pages,
    ),
    Fixture(
        "pdf-text-layers",
        "pdf",
        "One page for each OCR decision: text, glyphs with no Unicode mapping (A to J), a page "
        "number alone, no content, and one line on an A4 page and on a page of 5,000 points",
        _pdf_text_layers,
    ),
    Fixture(
        "pdf-non-english",
        "pdf",
        "Hebrew in logical and visual order, CJK, a character outside the BMP and combining marks, "
        "through a ToUnicode map",
        _pdf_non_english,
    ),
    Fixture(
        "pdf-encrypted",
        "pdf",
        "Encrypted with a user password (standard security handler, revision 2)",
        _pdf_encrypted,
    ),
    Fixture(
        "pdf-owner-password-only",
        "pdf",
        "Encrypted with an owner password only: opens without a password",
        _pdf_owner_password_only,
    ),
    Fixture(
        "pdf-truncated",
        "pdf",
        "The column-at-a-time PDF cut in half",
        _pdf_truncated,
    ),
    Fixture(
        "pdf-no-cross-reference",
        "pdf",
        "The column-at-a-time PDF cut just before its cross-reference table",
        _pdf_cross_reference_cut,
    ),
    Fixture(
        "pdf-garbage-body",
        "pdf",
        "A PDF header followed by bytes that are not PDF",
        _pdf_garbage_body,
    ),
    Fixture(
        "pdf-page-tree-overclaims",
        "pdf",
        "A page tree that claims three pages and holds one",
        _pdf_page_tree_overclaims,
    ),
    Fixture(
        "pdf-empty",
        "pdf",
        "A file of no bytes",
        lambda: b"",
    ),
    Fixture(
        "pdf-not-a-pdf",
        "pdf",
        "Text bytes with no PDF header",
        _not_a_pdf,
    ),
)


def _extraction_json(extraction: Extraction, stored: tuple[PageText, ...]) -> dict[str, object]:
    return {
        "extractor": extraction.extractor,
        "encoding": extraction.encoding,
        "pages": [
            _page_json(page, kept) for page, kept in zip(extraction.pages, stored, strict=True)
        ],
    }


def _page_json(page: Page, stored: PageText) -> dict[str, object]:
    """A page as the golden file records it; a PDF page also with its measurements and the OCR
    decision the stage stores for it (``vault.ingest.text_layer``)."""
    result: dict[str, object] = {
        "number": page.number,
        "text": page.text,
        "headings": [
            {
                "level": heading.level,
                "title": heading.title,
                "start": heading.start,
                "end": heading.end,
                # The text the offsets point at, so a reviewer can read them.
                "text_at_offsets": page.text[heading.start : heading.end],
            }
            for heading in page.headings
        ],
    }
    if page.layer is not None:
        result["layer"] = {
            "width": page.layer.width,
            "height": page.layer.height,
            "unmapped": page.layer.unmapped,
        }
        result["text_layer"] = stored.text_layer
    return result


def outcome(fixture: Fixture, data: bytes) -> dict[str, object]:
    """What the product's extractor gives for ``data``, as a golden file records it.

    Runs the sandbox entry point as the extraction stage does, checks a result with the stage's
    own check (what it would store), then runs the same function in this process; raises
    ``AssertionError`` if the two disagree.
    """
    args = (data,) if fixture.option is None else (data, fixture.option)
    try:
        direct: Extraction | ExtractionError = DIRECT[fixture.entry](*args)
    except ExtractionError as error:
        direct = error
    disagree = f"{fixture.name}: the sandbox and a direct call disagree"
    try:
        result = sandbox.run(fixture.entry, data, LIMITS, option=fixture.option)
    except sandbox.SandboxChildError as error:
        if not isinstance(direct, ExtractionError) or error.error_class != type(direct).__name__:
            raise AssertionError(disagree) from None
        reason = getattr(direct, "reason", None)
        return {
            "error": {"class": error.error_class, "reason": None if reason is None else str(reason)}
        }
    if not isinstance(result, Extraction) or result != direct:
        raise AssertionError(disagree)
    # Raises ExtractionResultError if the stage would refuse to store the result.
    _name, _encoding, stored = extract_stage._checked(result, fixture.extractor, fixture.fallback)
    return {"result": _extraction_json(result, stored)}


def golden(fixture: Fixture) -> dict[str, object]:
    """The golden file's content for ``fixture``, from the current fixture and extractor."""
    data = fixture.build()
    return {
        "fixture": fixture.name,
        "about": fixture.about,
        "type": fixture.ext,
        "entry": fixture.entry,
        "option": fixture.option,
        "input_bytes": len(data),
        "input_sha256": hashlib.sha256(data).hexdigest(),
        **outcome(fixture, data),
    }


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def dumps(content: dict[str, object]) -> str:
    """A golden file's text: UTF-8 JSON, indented, non-ASCII written as itself so it reads."""
    return json.dumps(content, ensure_ascii=False, indent=2) + "\n"


def write_all() -> list[str]:
    """Rewrite every golden file and remove those no fixture has. Returns the names written."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    names = {fixture.name for fixture in FIXTURES}
    for stale in GOLDEN_DIR.glob("*.json"):
        if stale.stem not in names:
            stale.unlink()
    for fixture in FIXTURES:
        with golden_path(fixture.name).open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(dumps(golden(fixture)))
    return sorted(names)


def main() -> int:
    written = write_all()
    print(f"Wrote {len(written)} golden files to tests/fixtures/extractors/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
