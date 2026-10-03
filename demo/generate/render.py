"""Renderers: turn a ``Document`` into bytes. Output is deterministic for pinned library versions.

Determinism rules: no timestamps (fixed dates, reportlab ``invariant``), no random ids, uncompressed
or fixed-timestamp containers, and every bit of noise comes from a ``random.Random(seed)``.
"""

from __future__ import annotations

import io
import random
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen.canvas import Canvas

from model import Block, Document

# --- text PDF -------------------------------------------------------------------------------------

_PAGE_W, _PAGE_H = 612.0, 792.0  # US Letter in points
_MARGIN = 72.0
_STYLES = {
    "h1": ("Helvetica-Bold", 18.0, 26.0, 8.0),
    "h2": ("Helvetica-Bold", 13.0, 18.0, 4.0),
    "p": ("Helvetica", 11.0, 15.0, 6.0),
    "li": ("Helvetica", 11.0, 15.0, 3.0),
}


def render_pdf_text(doc: Document) -> bytes:
    buf = io.BytesIO()
    canvas = Canvas(buf, pagesize=(_PAGE_W, _PAGE_H), pageCompression=0, invariant=1)
    canvas.setTitle(doc.exact_duplicate_of or doc.path)
    canvas.setAuthor("AI Document Vault demo corpus")
    canvas.setCreator("demo/generate")
    for page in doc.blocks:
        y = _PAGE_H - _MARGIN
        for block in page:
            y = _draw_block(canvas, block, y, doc.path)
        canvas.showPage()
    canvas.save()
    return buf.getvalue()


def _draw_block(canvas: Canvas, block: Block, y: float, where: str) -> float:
    font, size, leading, after = _STYLES[block.style]
    indent = 14.0 if block.style == "li" else 0.0
    width = _PAGE_W - 2 * _MARGIN - indent
    text = block.text
    lines = simpleSplit(text, font, size, width)
    canvas.setFont(font, size)
    for i, line in enumerate(lines):
        y -= leading
        if y < _MARGIN:
            raise ValueError(f"{where}: page overflows at {block.text[:40]!r}")
        if block.style == "li" and i == 0:
            canvas.drawString(_MARGIN, y, "-")
        canvas.drawString(_MARGIN + indent, y, line)
    return y - after


# --- scanned PDF ----------------------------------------------------------------------------------

_SCAN_W, _SCAN_H = 1275, 1650  # US Letter at 150 dpi


def render_pdf_scan(doc: Document) -> bytes:
    """A PDF whose pages are photographs of text: no text layer, so OCR is the only way in."""
    buf = io.BytesIO()
    canvas = Canvas(buf, pagesize=(_PAGE_W, _PAGE_H), pageCompression=0, invariant=1)
    canvas.setTitle(doc.exact_duplicate_of or doc.path)
    canvas.setAuthor("AI Document Vault demo corpus")
    canvas.setCreator("demo/generate")
    for number, lines in enumerate(doc.lines):
        image = _scan_page(lines, random.Random(doc.seed * 100 + number))
        jpeg = io.BytesIO()
        image.save(jpeg, "JPEG", quality=72, optimize=False, progressive=False)
        jpeg.seek(0)
        canvas.drawImage(ImageReader(jpeg), 0, 0, _PAGE_W, _PAGE_H)
        canvas.showPage()
    canvas.save()
    return buf.getvalue()


def _scan_page(lines: tuple[str, ...], rng: random.Random) -> Image.Image:
    return _scan_effects(scan_layout(lines).image, rng, max_degrees=1.2)


def scan_layout(lines: tuple[str, ...]) -> Pen:
    """Draw the lines of one scanned page, before the scanner effects."""
    pen = Pen(Image.new("L", (_SCAN_W, _SCAN_H), 246))
    title_font = _font(34)
    body_font = _font(28)
    y = 150
    for i, line in enumerate(lines):
        font = title_font if i < 2 and line.isupper() else body_font
        pen.text((130, y), line, 28, font)
        y += 62 if font is title_font else 50
    return pen


def _scan_effects(page: Image.Image, rng: random.Random, max_degrees: float) -> Image.Image:
    """Slight rotation, blur and speckle noise, like a flatbed scan of a printed page."""
    degrees = rng.uniform(-max_degrees, max_degrees)
    page = page.rotate(degrees, resample=Image.Resampling.BICUBIC, fillcolor=238)
    page = page.filter(ImageFilter.GaussianBlur(0.6))
    width, height = page.size
    data = bytearray(page.tobytes())
    for _ in range(width * height // 90):
        i = rng.randrange(width * height)
        data[i] = max(0, min(255, data[i] + rng.randint(-45, 25)))
    return Image.frombytes("L", page.size, bytes(data))


# --- DOCX -----------------------------------------------------------------------------------------

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    "</Relationships>"
)
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
    "</Relationships>"
)
_STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<w:styles {_NS}>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    '<w:pPr><w:spacing w:after="120"/></w:pPr><w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:keepNext/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'
    "</w:styles>"
)
_NUMBERING_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<w:numbering {_NS}>"
    '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>'
    '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    "</w:numbering>"
)
_CORE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<dc:title>{title}</dc:title><dc:creator>AI Document Vault demo corpus</dc:creator>"
    '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
    "</cp:coreProperties>"
)
_STYLE_IDS = {"h1": "Heading1", "h2": "Heading2", "p": "Normal", "li": "ListBullet"}
_ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def render_docx(doc: Document) -> bytes:
    paragraphs = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{_STYLE_IDS[b.style]}"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{escape(b.text)}</w:t></w:r></w:p>'
        for page in doc.blocks
        for b in page
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_NS}><w:body>{paragraphs}"
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'
        "</w:sectPr></w:body></w:document>"
    )
    parts = [  # [Content_Types].xml first, as Word writes it
        ("[Content_Types].xml", _CONTENT_TYPES),
        ("_rels/.rels", _ROOT_RELS),
        ("word/document.xml", document),
        ("word/_rels/document.xml.rels", _DOC_RELS),
        ("word/styles.xml", _STYLES_XML),
        ("word/numbering.xml", _NUMBERING_XML),
        ("docProps/core.xml", _CORE_XML.format(title=escape(doc.exact_duplicate_of or doc.path))),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in parts:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buf.getvalue()


# --- screenshots and receipts ---------------------------------------------------------------------

Box = tuple[float, float, float, float]  # left, top, right, bottom


@dataclass(frozen=True)
class Placed:
    """A piece of text as drawn: where it landed, and the box it must stay inside, if any."""

    text: str
    box: Box
    within: Box | None


class Pen:
    """Draws text on an image and records where every piece landed, so tests can prove that
    nothing overlaps, runs off the picture or leaves its box (OCR ground truth must be legible)."""

    def __init__(self, image: Image.Image) -> None:
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.placed: list[Placed] = []

    def text(
        self,
        xy: tuple[float, float],
        text: str,
        fill: int | tuple[int, int, int],
        font: ImageFont.FreeTypeFont,
        within: Box | None = None,
    ) -> None:
        self.draw.text(xy, text, fill=fill, font=font)
        self.placed.append(Placed(text, self.draw.textbbox(xy, text, font=font), within))

    def width(self, text: str, font: ImageFont.FreeTypeFont) -> float:
        return float(self.draw.textlength(text, font=font))


def render_image(doc: Document) -> bytes:
    pen = image_layout(doc)
    image = pen.image
    if doc.layout == "receipt":
        image = _photo_of(image, random.Random(doc.seed))
    buf = io.BytesIO()
    if doc.kind == "jpg":
        image.convert("RGB").save(buf, "JPEG", quality=80, optimize=False, progressive=False)
    else:
        image.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def image_layout(doc: Document) -> Pen:
    """Draw a screenshot or receipt, before any photo effects."""
    drawers = {
        "terminal": _terminal,
        "kanban": _kanban,
        "calendar": _calendar,
        "chat": _chat,
        "receipt": _receipt,
    }
    return drawers[doc.layout](doc.lines[0])


def _font(size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.load_default(size=size)
    assert isinstance(font, ImageFont.FreeTypeFont)  # noqa: S101 - the bundled font is scalable
    return font


def _terminal(lines: tuple[str, ...]) -> Pen:
    font = _font(24)
    pen = Pen(Image.new("RGB", (1100, 90 + 40 * len(lines)), (24, 26, 32)))
    pen.draw.rectangle((0, 0, 1100, 44), fill=(52, 56, 66))
    pen.text((16, 8), "Terminal", (200, 204, 214), _font(22))
    for i, line in enumerate(lines):
        if line.startswith("ERROR") or "failed" in line.lower() or line.startswith("Module"):
            colour = (255, 120, 110)
        elif "$" in line[:30]:
            colour = (130, 230, 150)
        else:
            colour = (225, 228, 235)
        pen.text((24, 66 + 40 * i), line, colour, font)
    return pen


def _kanban(lines: tuple[str, ...]) -> Pen:
    title, *rest = lines
    columns: list[tuple[str, list[str]]] = []
    for line in rest:
        if line.isupper():
            columns.append((line, []))
        else:
            columns[-1][1].append(line)
    width, col_w = 1230, 380
    height = 170 + 110 * max(len(cards) for _, cards in columns)
    pen = Pen(Image.new("RGB", (width, height), (240, 242, 246)))
    pen.text((30, 24), title, (30, 40, 60), _font(34))
    for c, (heading, cards) in enumerate(columns):
        x = 30 + c * (col_w + 15)
        column = (x, 90, x + col_w, height - 20)
        pen.draw.rounded_rectangle(column, 12, fill=(222, 226, 234))
        pen.text((x + 18, 104), heading, (60, 70, 90), _font(24), within=column)
        for r, card in enumerate(cards):
            top = 150 + r * 110
            box = (x + 14, top, x + col_w - 14, top + 90)
            pen.draw.rounded_rectangle(box, 8, fill=(255, 255, 255))
            # 14 px of padding on both sides of the text inside the card
            for k, row in enumerate(_wrap(pen, card, _font(22), col_w - 28 - 28)):
                pen.text((x + 28, top + 14 + 30 * k), row, (30, 36, 50), _font(22), within=box)
    return pen


def _calendar(lines: tuple[str, ...]) -> Pen:
    title, *events = lines
    pen = Pen(Image.new("RGB", (1100, 150 + 90 * len(events)), (255, 255, 255)))
    pen.draw.rectangle((0, 0, 1100, 90), fill=(40, 90, 160))
    pen.text((30, 24), title, (255, 255, 255), _font(36))
    for i, event in enumerate(events):
        top = 110 + 90 * i
        box = (30, top, 1070, top + 70)
        pen.draw.rounded_rectangle(box, 10, fill=(226, 238, 252))
        pen.text((50, top + 18), event, (20, 30, 50), _font(28), within=box)
    return pen


def _chat(lines: tuple[str, ...]) -> Pen:
    date, *messages = lines
    font = _font(26)
    bubble_w, padding = 640, 20
    probe = Pen(Image.new("RGB", (1, 1)))
    wrapped = [_wrap(probe, m, font, bubble_w - 2 * padding) for m in messages]
    height = 120 + sum(40 * len(w) + 50 for w in wrapped)
    pen = Pen(Image.new("RGB", (1000, height), (236, 238, 243)))
    pen.text((400, 24), date, (110, 116, 130), _font(24))
    y = 80
    for message, rows in zip(messages, wrapped, strict=True):
        mine = message.startswith("Alex:")
        box_h = 40 * len(rows) + 24
        x0 = 330 if mine else 30
        box = (x0, y, x0 + bubble_w, y + box_h)
        pen.draw.rounded_rectangle(box, 16, fill=(70, 130, 230) if mine else (255, 255, 255))
        for k, row in enumerate(rows):
            colour = (255, 255, 255) if mine else (25, 30, 45)
            pen.text((x0 + padding, y + 14 + 40 * k), row, colour, font, within=box)
        y += box_h + 26
    return pen


def _receipt(lines: tuple[str, ...]) -> Pen:
    font = _font(28)
    width, margin, gap, row_h = 780, 30, 24, 48
    probe = Pen(Image.new("L", (1, 1)))
    # Lay the rows out first: a long item name is wrapped so that it never reaches the price.
    rows: list[tuple[list[str], str | None]] = []
    for line in lines:
        left, _, amount = line.partition("\t")
        room = width - 2 * margin - (probe.width(amount, font) + gap if amount else 0)
        rows.append((_wrap(probe, left, font, room), amount or None))
    height = 100 + row_h * sum(len(parts) for parts, _ in rows)
    pen = Pen(Image.new("L", (width, height), 250))
    y = 50
    for parts, right in rows:
        for k, part in enumerate(parts):
            pen.text((margin, y + row_h * k), part, 30, font)
        if right is not None:
            pen.text((width - margin - pen.width(right, font), y), right, 30, font)
        y += row_h * len(parts)
    return pen


def _photo_of(paper: Image.Image, rng: random.Random) -> Image.Image:
    """Put the receipt on a darker table, slightly rotated, with noise, like a phone photo."""
    table = Image.new("L", (paper.width + 160, paper.height + 160), 120)
    paper = paper.rotate(
        rng.uniform(-2.0, 2.0), resample=Image.Resampling.BICUBIC, expand=True, fillcolor=120
    )
    table.paste(paper, ((table.width - paper.width) // 2, (table.height - paper.height) // 2))
    return _scan_effects(table, rng, max_degrees=0.0)


def _wrap(pen: Pen, text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    rows: list[str] = []
    current = ""
    for word in text.split(" "):
        trial = f"{current} {word}".strip()
        if current and pen.width(trial, font) > max_width:
            rows.append(current)
            current = word
        else:
            current = trial
    rows.append(current)
    return rows
