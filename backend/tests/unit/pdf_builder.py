"""Synthetic PDFs for the PDF extractor's tests, written byte by byte.

Small and fully controlled: pages with text in a standard font (Helvetica, no font file), pages
with no text layer, an optional ``ToUnicode`` map (to give PDFium any characters), an optional
font encoding with glyph names PDFium does not know (glyphs with no Unicode mapping), pages of any
size, a page tree that claims more pages than it holds, a document information dictionary
(``Info``, its strings given as bytes and encrypted with the file, or its source written as is),
an outline (bookmarks with a title, a destination or an action, children, and a loop on request),
and the standard security handler (revision 2, RC4 with a 40-bit
key, ISO 32000-1 section 7.6.3), with a user password or an owner password only.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass

# The padding string of the standard security handler (ISO 32000-1, 7.6.3.3, algorithm 2).
_PAD: bytes = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_FILE_ID: bytes = hashlib.md5(b"synthetic test file").digest()  # noqa: S324 (the format's own hash)
_PERMISSIONS: int = -4


def rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def _padded(password: bytes) -> bytes:
    return (password + _PAD)[:32]


def text_stream(lines: list[str], size: int = 12) -> bytes:
    """A content stream that shows ``lines`` (Latin-1) one under the other."""
    parts = [b"BT", f"/F1 {size} Tf".encode(), b"72 720 Td", b"14 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        parts.append(b"(" + escaped.encode("latin-1") + b") Tj T*")
    parts.append(b"ET")
    return b"\n".join(parts)


def to_unicode_map(mapping: dict[int, str]) -> bytes:
    """A ``ToUnicode`` CMap from one-byte codes to UTF-16BE hex (``"D83DDE00"``)."""
    entries = b" ".join(f"<{code:02X}> <{target}>".encode() for code, target in mapping.items())
    return (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T def\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        + f"{len(mapping)} beginbfchar ".encode()
        + entries
        + b" endbfchar endcmap CMapName currentdict /CMap defineresource pop end end"
    )


def utf16(text: str) -> bytes:
    """A PDF text string in UTF-16BE with its byte order mark."""
    return b"\xfe\xff" + text.encode("utf-16-be")


@dataclass(frozen=True)
class Bookmark:
    """An outline item. ``title`` is the bytes of its title string (written in hex). ``page`` gives
    it the destination ``[<that page> 0 R /XYZ 0 792 0]``, by index into the pages; ``dest`` and
    ``action`` are a ``Dest`` and an ``A`` entry written as is, where ``{page0}``, ``{page1}`` ...
    stand for the page objects' numbers. ``kids`` are its children, in order."""

    title: bytes
    page: int | None = None
    dest: str | None = None
    action: str | None = None
    kids: tuple[Bookmark, ...] = ()


def build(
    pages: list[bytes | None],
    *,
    user_password: bytes | None = None,
    owner_password: bytes = b"owner",
    security_filter: str = "Standard",
    to_unicode: bytes | None = None,
    claimed_pages: int | None = None,
    unknown_glyphs: range | None = None,
    sizes: Sequence[tuple[float, float]] | None = None,
    info: dict[str, bytes] | None = None,
    info_source: bytes | None = None,
    outline: Sequence[Bookmark] | None = None,
    outline_loop: str | None = None,
    catalog_extra: str = "",
) -> bytes:
    """A PDF with one page per item of ``pages``: a content stream, or None for a page with no
    content at all. ``user_password`` encrypts the file (``b""`` for an owner password only);
    ``claimed_pages`` is the page count the page tree claims. ``unknown_glyphs`` gives the font an
    encoding that names each of those one-byte codes with a glyph name PDFium does not know
    (``/g1``, ``/g2`` ...), so PDFium has no Unicode for them and gives their raw codes. ``sizes``
    is each page's width and height in points (US Letter, 612 by 792, by default). ``info`` gives
    the trailer an ``Info`` dictionary whose entries are strings of exactly these bytes (written
    in hex, and encrypted when the file is); ``info_source`` gives it one written as is instead
    (names, numbers, a key twice), never encrypted. ``outline`` gives the catalog an outline of
    these top-level bookmarks; ``outline_loop`` makes it loop: ``"sibling"`` points the last
    top-level bookmark's ``Next`` at the first, ``"child"`` makes the first top-level bookmark
    the only child of its own first child. ``catalog_extra`` is written into the catalog as is
    (a ``Dests`` dictionary of named destinations, say), with the same ``{pageN}`` stand-ins.
    The outline is never encrypted, so it is for files without a password."""
    objects: dict[int, bytes] = {}
    streams: dict[int, bytes] = {}
    catalog, tree, font = 1, 2, 3
    next_id = 4
    kids: list[int] = []
    for position, content in enumerate(pages):
        width, height = (612, 792) if sizes is None else sizes[position]
        page_id, next_id = next_id, next_id + 1
        kids.append(page_id)
        resources = ""
        if content is not None:
            content_id, next_id = next_id, next_id + 1
            streams[content_id] = content
            resources = f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content_id} 0 R "
        objects[page_id] = (
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {width} {height}] {resources}>>"
        ).encode()
    refs = {f"page{n}": kid for n, kid in enumerate(kids)}
    outline_entry = ""
    if outline is not None:
        root, next_id = next_id, next_id + 1
        next_id = _write_outline(objects, root, list(outline), next_id, kids, refs, outline_loop)
        outline_entry = f" /Outlines {root} 0 R"
    extra = f" {catalog_extra.format(**refs)}" if catalog_extra else ""
    objects[catalog] = f"<< /Type /Catalog /Pages {tree} 0 R{outline_entry}{extra} >>".encode()
    count = len(pages) if claimed_pages is None else claimed_pages
    kid_list = " ".join(f"{kid} 0 R" for kid in kids)
    objects[tree] = f"<< /Type /Pages /Kids [{kid_list}] /Count {count} >>".encode()
    font_entries = "/Type /Font /Subtype /Type1 /BaseFont /Helvetica"
    if unknown_glyphs is not None:
        names = " ".join(f"/g{number}" for number in range(1, len(unknown_glyphs) + 1))
        font_entries += f" /Encoding << /Differences [{unknown_glyphs.start} {names}] >>"
    if to_unicode is not None:
        map_id, next_id = next_id, next_id + 1
        streams[map_id] = to_unicode
        font_entries += f" /ToUnicode {map_id} 0 R"
    objects[font] = f"<< {font_entries} >>".encode()
    info_id: int | None = None
    if info is not None or info_source is not None:
        info_id, next_id = next_id, next_id + 1

    key: bytes | None = None
    encrypt_id: int | None = None
    if user_password is not None:
        owner_key = hashlib.md5(_padded(owner_password)).digest()[:5]  # noqa: S324
        owner_value = rc4(owner_key, _padded(user_password))
        key = hashlib.md5(  # noqa: S324
            _padded(user_password) + owner_value + struct.pack("<i", _PERMISSIONS) + _FILE_ID
        ).digest()[:5]
        user_value = rc4(key, _PAD)
        encrypt_id, next_id = next_id, next_id + 1
        objects[encrypt_id] = (
            f"<< /Filter /{security_filter} /V 1 /R 2 /O <{owner_value.hex()}> "
            f"/U <{user_value.hex()}> /P {_PERMISSIONS} >>"
        ).encode()

    def encrypted(number: int, data: bytes) -> bytes:
        if key is None:
            return data
        object_key = hashlib.md5(  # noqa: S324
            key + struct.pack("<I", number)[:3] + b"\x00\x00"
        ).digest()[: len(key) + 5]
        return rc4(object_key, data)

    if info_id is not None:
        if info_source is not None:
            objects[info_id] = info_source
        else:
            assert info is not None
            entries = b" ".join(
                f"/{name} <{encrypted(info_id, value).hex()}>".encode()
                for name, value in info.items()
            )
            objects[info_id] = b"<< " + entries + b" >>"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects.keys() | streams.keys()):
        offsets[number] = len(out)
        if number in streams:
            data = encrypted(number, streams[number])
            body = f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"
        else:
            body = objects[number]
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    size = max(offsets) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    trailer = f"<< /Size {size} /Root {catalog} 0 R /ID [<{_FILE_ID.hex()}> <{_FILE_ID.hex()}>]"
    if encrypt_id is not None:
        trailer += f" /Encrypt {encrypt_id} 0 R"
    if info_id is not None:
        trailer += f" /Info {info_id} 0 R"
    out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _write_outline(
    objects: dict[int, bytes],
    root: int,
    items: list[Bookmark],
    next_id: int,
    page_ids: list[int],
    refs: dict[str, int],
    loop: str | None,
) -> int:
    """Writes the outline dictionary ``root`` and its items; returns the next free object number."""
    numbers: dict[int, int] = {}  # id(item) -> object number, in preorder

    def number(item: Bookmark) -> None:
        nonlocal next_id
        numbers[id(item)], next_id = next_id, next_id + 1
        for kid in item.kids:
            number(kid)

    if not items:
        objects[root] = b"<< /Type /Outlines /Count 0 >>"
        return next_id
    for item in items:
        number(item)
    links: dict[int, dict[str, int]] = {}

    def link(parent: int, level: list[Bookmark]) -> None:
        for position, item in enumerate(level):
            entries = links.setdefault(numbers[id(item)], {})
            entries["Parent"] = parent
            if position > 0:
                entries["Prev"] = numbers[id(level[position - 1])]
            if position + 1 < len(level):
                entries["Next"] = numbers[id(level[position + 1])]
            if item.kids:
                entries["First"] = numbers[id(item.kids[0])]
                entries["Last"] = numbers[id(item.kids[-1])]
                link(numbers[id(item)], list(item.kids))

    link(root, items)
    if loop == "sibling":
        links[numbers[id(items[-1])]]["Next"] = numbers[id(items[0])]
    elif loop == "child":
        child = numbers[id(items[0].kids[0])]
        links[child]["First"] = links[child]["Last"] = numbers[id(items[0])]
    elif loop is not None:
        raise ValueError(loop)

    def write(item: Bookmark) -> None:
        parts = [f"/Title <{item.title.hex()}>"]
        parts += [f"/{key} {value} 0 R" for key, value in links[numbers[id(item)]].items()]
        if item.page is not None:
            parts.append(f"/Dest [{page_ids[item.page]} 0 R /XYZ 0 792 0]")
        if item.dest is not None:
            parts.append(f"/Dest {item.dest.format(**refs)}")
        if item.action is not None:
            parts.append(f"/A {item.action.format(**refs)}")
        objects[numbers[id(item)]] = f"<< {' '.join(parts)} >>".encode()
        for kid in item.kids:
            write(kid)

    for item in items:
        write(item)
    first, last = numbers[id(items[0])], numbers[id(items[-1])]
    objects[root] = (
        f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {len(numbers)} >>"
    ).encode()
    return next_id
