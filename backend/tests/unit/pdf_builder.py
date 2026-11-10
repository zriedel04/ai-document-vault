"""Synthetic PDFs for the PDF extractor's tests, written byte by byte.

Small and fully controlled: pages with text in a standard font (Helvetica, no font file), pages
with no text layer, an optional ``ToUnicode`` map (to give PDFium any characters), a page tree that
claims more pages than it holds, and the standard security handler (revision 2, RC4 with a 40-bit
key, ISO 32000-1 section 7.6.3), with a user password or an owner password only.
"""

from __future__ import annotations

import hashlib
import struct

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


def build(
    pages: list[bytes | None],
    *,
    user_password: bytes | None = None,
    owner_password: bytes = b"owner",
    security_filter: str = "Standard",
    to_unicode: bytes | None = None,
    claimed_pages: int | None = None,
) -> bytes:
    """A PDF with one page per item of ``pages``: a content stream, or None for a page with no
    content at all. ``user_password`` encrypts the file (``b""`` for an owner password only);
    ``claimed_pages`` is the page count the page tree claims."""
    objects: dict[int, bytes] = {}
    streams: dict[int, bytes] = {}
    catalog, tree, font = 1, 2, 3
    next_id = 4
    kids: list[int] = []
    for content in pages:
        page_id, next_id = next_id, next_id + 1
        kids.append(page_id)
        resources = ""
        if content is not None:
            content_id, next_id = next_id, next_id + 1
            streams[content_id] = content
            resources = f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content_id} 0 R "
        objects[page_id] = (
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 612 792] {resources}>>".encode()
        )
    objects[catalog] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    count = len(pages) if claimed_pages is None else claimed_pages
    kid_list = " ".join(f"{kid} 0 R" for kid in kids)
    objects[tree] = f"<< /Type /Pages /Kids [{kid_list}] /Count {count} >>".encode()
    font_entries = "/Type /Font /Subtype /Type1 /BaseFont /Helvetica"
    if to_unicode is not None:
        map_id, next_id = next_id, next_id + 1
        streams[map_id] = to_unicode
        font_entries += f" /ToUnicode {map_id} 0 R"
    objects[font] = f"<< {font_entries} >>".encode()

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

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects.keys() | streams.keys()):
        offsets[number] = len(out)
        if number in streams:
            data = streams[number]
            if key is not None:
                object_key = hashlib.md5(  # noqa: S324
                    key + struct.pack("<I", number)[:3] + b"\x00\x00"
                ).digest()[: len(key) + 5]
                data = rc4(object_key, data)
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
    out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
