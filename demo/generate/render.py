"""Renderers: turn a ``Document`` into bytes. Output is deterministic for pinned library versions.

Determinism rules: no timestamps (fixed dates, reportlab ``invariant``), no random ids, uncompressed
or fixed-timestamp containers, and every bit of noise comes from a ``random.Random(seed)``.
"""

from __future__ import annotations

import io
import random
import zipfile
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
    page = Image.new("L", (_SCAN_W, _SCAN_H), 246)
    draw = ImageDraw.Draw(page)
    title_font = ImageFont.load_default(size=34)
    body_font = ImageFont.load_default(size=28)
    y = 150
    for i, line in enumerate(lines):
        font = title_font if i < 2 and line.isupper() else body_font
        draw.text((130, y), line, fill=28, font=font)
        y += 62 if font is title_font else 50
    return _scan_effects(page, rng, max_degrees=1.2)


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


def render_image(doc: Document) -> bytes:
    lines = doc.lines[0]
    drawers = {
        "terminal": _terminal,
        "kanban": _kanban,
        "calendar": _calendar,
        "chat": _chat,
        "receipt": _receipt,
    }
    image = drawers[doc.layout](lines, random.Random(doc.seed))
    buf = io.BytesIO()
    if doc.kind == "jpg":
        image.convert("RGB").save(buf, "JPEG", quality=80, optimize=False, progressive=False)
    else:
        image.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def _font(size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.load_default(size=size)
    assert isinstance(font, ImageFont.FreeTypeFont)  # noqa: S101 - the bundled font is scalable
    return font


def _terminal(lines: tuple[str, ...], rng: random.Random) -> Image.Image:
    del rng
    font = _font(24)
    image = Image.new("RGB", (1100, 90 + 40 * len(lines)), (24, 26, 32))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1100, 44), fill=(52, 56, 66))
    draw.text((16, 8), "Terminal", fill=(200, 204, 214), font=_font(22))
    for i, line in enumerate(lines):
        if line.startswith("ERROR") or "failed" in line.lower() or line.startswith("Module"):
            colour = (255, 120, 110)
        elif "$" in line[:30]:
            colour = (130, 230, 150)
        else:
            colour = (225, 228, 235)
        draw.text((24, 66 + 40 * i), line, fill=colour, font=font)
    return image


def _kanban(lines: tuple[str, ...], rng: random.Random) -> Image.Image:
    del rng
    title, *rest = lines
    columns: list[tuple[str, list[str]]] = []
    for line in rest:
        if line.isupper():
            columns.append((line, []))
        else:
            columns[-1][1].append(line)
    width, col_w = 1230, 380
    height = 170 + 110 * max(len(cards) for _, cards in columns)
    image = Image.new("RGB", (width, height), (240, 242, 246))
    draw = ImageDraw.Draw(image)
    draw.text((30, 24), title, fill=(30, 40, 60), font=_font(34))
    for c, (heading, cards) in enumerate(columns):
        x = 30 + c * (col_w + 15)
        draw.rounded_rectangle((x, 90, x + col_w, height - 20), 12, fill=(222, 226, 234))
        draw.text((x + 18, 104), heading, fill=(60, 70, 90), font=_font(24))
        for r, card in enumerate(cards):
            top = 150 + r * 110
            draw.rounded_rectangle((x + 14, top, x + col_w - 14, top + 90), 8, fill=(255, 255, 255))
            wrapped = _wrap(draw, card, _font(22), col_w - 60)
            for k, row in enumerate(wrapped):
                draw.text((x + 28, top + 14 + 30 * k), row, fill=(30, 36, 50), font=_font(22))
    return image


def _calendar(lines: tuple[str, ...], rng: random.Random) -> Image.Image:
    del rng
    title, *events = lines
    image = Image.new("RGB", (1100, 150 + 90 * len(events)), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1100, 90), fill=(40, 90, 160))
    draw.text((30, 24), title, fill=(255, 255, 255), font=_font(36))
    for i, event in enumerate(events):
        top = 110 + 90 * i
        draw.rounded_rectangle((30, top, 1070, top + 70), 10, fill=(226, 238, 252))
        draw.text((50, top + 18), event, fill=(20, 30, 50), font=_font(28))
    return image


def _chat(lines: tuple[str, ...], rng: random.Random) -> Image.Image:
    del rng
    date, *messages = lines
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = _font(26)
    wrapped = [_wrap(probe, m, font, 640) for m in messages]
    height = 120 + sum(40 * len(w) + 50 for w in wrapped)
    image = Image.new("RGB", (1000, height), (236, 238, 243))
    draw = ImageDraw.Draw(image)
    draw.text((400, 24), date, fill=(110, 116, 130), font=_font(24))
    y = 80
    for message, rows in zip(messages, wrapped, strict=True):
        mine = message.startswith("Alex:")
        box_h = 40 * len(rows) + 24
        x0, x1 = (330, 970) if mine else (30, 670)
        draw.rounded_rectangle(
            (x0, y, x1, y + box_h), 16, fill=(70, 130, 230) if mine else (255, 255, 255)
        )
        for k, row in enumerate(rows):
            draw.text(
                (x0 + 20, y + 14 + 40 * k),
                row,
                fill=(255, 255, 255) if mine else (25, 30, 45),
                font=font,
            )
        y += box_h + 26
    return image


def _receipt(lines: tuple[str, ...], rng: random.Random) -> Image.Image:
    font = _font(28)
    width, height = 620, 130 + 48 * len(lines)
    paper = Image.new("L", (width, height), 250)
    draw = ImageDraw.Draw(paper)
    for i, line in enumerate(lines):
        y = 50 + 48 * i
        if "\t" in line:
            left, right = line.split("\t")
            draw.text((30, y), left, fill=30, font=font)
            draw.text(
                (width - 30 - draw.textlength(right, font=font), y), right, fill=30, font=font
            )
        else:
            draw.text((30, y), line, fill=30, font=font)
    # Put the receipt on a darker table, slightly rotated, with noise, like a phone photo.
    table = Image.new("L", (width + 160, height + 160), 120)
    paper = paper.rotate(
        rng.uniform(-2.0, 2.0), resample=Image.Resampling.BICUBIC, expand=True, fillcolor=120
    )
    table.paste(paper, ((table.width - paper.width) // 2, (table.height - paper.height) // 2))
    return _scan_effects(table, rng, max_degrees=0.0)


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: float
) -> list[str]:
    rows: list[str] = []
    current = ""
    for word in text.split(" "):
        trial = f"{current} {word}".strip()
        if current and draw.textlength(trial, font=font) > max_width:
            rows.append(current)
            current = word
        else:
            current = trial
    rows.append(current)
    return rows
