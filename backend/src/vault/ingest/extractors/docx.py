"""DOCX: the body's paragraphs and tables in document order, with headings from the built-in styles.

A DOCX is a zip file, and python-docx opens it with ``zipfile``, so the blob is checked before the
library sees it:

1. The validator's DOCX rule runs on the bytes (``validate.check_docx``): size, entry count and
   directory size from the end records before ``zipfile`` opens anything, then encrypted entries,
   member names, compression method, names used twice, the total size and the compression ratio.
   An OLE compound file (a legacy or password-protected Office file) is refused there too.
2. Those sizes are the archive's own claims, and ``zipfile`` reads a member whole in one call
   (``ZipFile.read``), decompressing as much as the deflate stream holds before cutting it to the
   claimed size. A member whose stream is far longer than its claim would fill memory first. So
   every member is read here in small pieces, never past its claimed size, and copied into a new,
   uncompressed zip in memory. python-docx opens only that copy, which holds exactly the bytes the
   validator bounded.

What the text holds:

- Paragraphs and tables of the main body, in order, including those inside content controls.
  Paragraphs are separated by a blank line (``\\n\\n``), like Markdown, and a line break inside a
  paragraph is ``\\n``, so the chunker can tell the two apart. Empty paragraphs are left out.
- A table is one block: one line per row, cells separated by a tab. Inside a cell, paragraphs,
  line breaks and tabs become single spaces so that a row stays one line, and a nested table is
  flattened into its cell the same way. A row whose cells are all empty, and a row deleted as a
  tracked change, are left out, and so is a cell deleted as a tracked change (``w:cellDel``), so
  the cells after it move up a column as they do when the change is accepted.
- Tracked changes are read as if accepted: inserted and moved-in text is kept, deleted and
  moved-out text is left out. That includes the co-authoring conflicts Word 2010 added: text a
  conflict deleted (``w14:conflictDel``) is left out and text it inserted (``w14:conflictIns``)
  is kept. A deleted paragraph mark still ends its paragraph.
- Text boxes are kept: each box's paragraphs follow the paragraph that anchors the box. Word writes
  a box twice (a DrawingML shape and a VML copy for old readers, in ``mc:AlternateContent``), so
  one representation is read: the first ``mc:Choice`` whose ``Requires`` names only namespaces
  this extractor understands (``UNDERSTOOD``: WordprocessingML, its Word 2010 extensions, and
  Word's shapes, shape groups and drawing canvases, whose text boxes it reads), or else the
  ``mc:Fallback``. The same rule
  applies wherever alternate content wraps a paragraph, a table, a row or a cell.
- Markup Compatibility (ECMA-376 Part 3) is applied to the document and the styles before they are
  read, as by a consumer that understands ``UNDERSTOOD``: an element from a namespace declared
  ``mc:Ignorable`` on it or any ancestor (the document root included), and not understood, is left
  out with everything in it, unless ``mc:ProcessContent`` in scope names it, and then its content
  is read in its place. Prefixes are resolved where the attribute is written. Word declares its
  later extensions ignorable in every document; none of them holds body text (see
  ``UNDERSTOOD``). ``mc:MustUnderstand`` naming a namespace not understood, and compatibility
  markup the standard makes an error, are refused (``CompatibilityError``).
- Inside a paragraph, every element is read through unless ``EXCLUDED`` names it, so text inside a
  container is kept whatever the container: hyperlinks, smart tags, custom XML, inline content
  controls, simple fields, insertions and moves (``w:ins``, ``w:moveTo``), text direction
  (``w:dir``, ``w:bdo``), the base text of ruby (``w:rubyBase``), and any wrapper not listed here.
  Text comes from ``w:t``; tabs (``w:tab``, ``w:ptab``) become a tab, breaks (``w:br``, ``w:cr``) a
  line break, a non-breaking hyphen a hyphen, and a soft hyphen nothing.
- Field results are kept and field codes are not (``w:instrText``, and everything between a
  complex field's ``begin`` and ``separate``, a field nested in the code included). A field marker
  (``w:fldChar``) counts wherever the text around it is left out because it is not shown: in a
  hidden run, a ruby guide or an equation (``FIELDS_ONLY``). A hidden ``separate`` still ends the
  code, so the result after it is read, and a hidden ``begin`` still starts one, so a nested
  field's result inside the code stays out. A marker in content that accepting the changes removes
  (``w:del``, ``w:moveFrom``, ``w14:conflictDel``), or in a representation of alternate content
  that is not read, does not count: the document as accepted and read has no such marker.
- Ruby's pronunciation guide (``w:rt``, the small kana over Japanese text) is left out, so the base
  text reads as written.
- A symbol (``w:sym``) is left out: it names a glyph in a symbol font (Wingdings, Symbol), usually a
  private-use code point, not a character.
- Hidden text (``w:vanish`` set on the run) is left out, and so is a text box anchored in a
  hidden run. Text hidden through a style is not detected and is kept.
- Left out: headers and footers (letterhead and page numbers repeated on every page), footnotes,
  endnotes and comments (separate parts, not the body), equations (``m:oMath``: their linear text
  would lose the structure that gives it meaning), content control properties, and the document's
  properties (title, author, dates: the metadata stage reads those later). Runs placed directly in
  the body or a cell, outside any paragraph (allowed by the schema for tracked changes, not written
  by Word), are not read.

Headings are paragraphs in the body (not in a table or a text box) whose paragraph style is one of
the built-in ``Heading 1`` to ``Heading 9`` (levels 1 to 9) or ``Title`` (level 1), matched by the
style's name, which Word writes in English whatever the language of its interface. A heading spans
its paragraph's text, so ``text[start:end]`` is the heading as stored, and its title is that text
with white space collapsed. A style that only looks like a heading, or is based on one, is not one.

Content imported from another file (``w:altChunk``: HTML, RTF or another document kept as a
separate part, which Word converts when it opens the file) is not read, so a file that holds any,
in the body, a table cell, a content control or a text box, is refused (``ImportedContentError``)
rather than reported as read without it.

A Strict Open XML document (Word's "Strict Open XML Document" format, other namespaces) is refused
rather than read as empty.

DOCX has no reliable pages (Word lays them out when it renders), so the whole text is one page, as
for text files. The section anchors in the text view come from the headings.

Errors are ``NotDocxError``, ``ImportedContentError`` or ``CompatibilityError`` with fixed text
naming a reason, never the file's name or content, and without the library's exception attached,
because a library message can quote a part of the file.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Final

from docx import Document
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement

from vault.ingest.extractors import Extraction, ExtractionError, Heading, Page
from vault.ingest.validate import DEFAULT_LIMITS, Limits, Reason, check_docx

VERSION: Final = "docx-1"
READ_CHUNK: Final = 64 * 1024
PARAGRAPH_BREAK: Final = "\n\n"
CELL_SEPARATOR: Final = "\t"
ROW_SEPARATOR: Final = "\n"

_MC: Final = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_M: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_W14: Final = "{http://schemas.microsoft.com/office/word/2010/wordml}"
_DOCUMENT, _BODY = qn("w:document"), qn("w:body")
_P, _TBL, _TR, _TC, _R = qn("w:p"), qn("w:tbl"), qn("w:tr"), qn("w:tc"), qn("w:r")
_SDT, _SDT_CONTENT, _CUSTOM_XML = qn("w:sdt"), qn("w:sdtContent"), qn("w:customXml")
_DEL, _T, _TXBX_CONTENT = qn("w:del"), qn("w:t"), qn("w:txbxContent")
_FLD_CHAR, _FLD_CHAR_TYPE = qn("w:fldChar"), qn("w:fldCharType")
_RPR, _VANISH, _VAL = qn("w:rPr"), qn("w:vanish"), qn("w:val")
_PPR, _PSTYLE, _TRPR = qn("w:pPr"), qn("w:pStyle"), qn("w:trPr")
_TCPR, _CELL_DEL = qn("w:tcPr"), qn("w:cellDel")
_STYLE, _STYLE_ID, _NAME, _TYPE = qn("w:style"), qn("w:styleId"), qn("w:name"), qn("w:type")
_ALTERNATE, _CHOICE, _FALLBACK = (
    f"{_MC}AlternateContent",
    f"{_MC}Choice",
    f"{_MC}Fallback",
)
_ALT_CHUNK: Final = qn("w:altChunk")
_MC_NAMESPACE: Final = _MC[1:-1]
_IGNORABLE, _PROCESS_CONTENT, _MUST_UNDERSTAND = (
    f"{_MC}Ignorable",
    f"{_MC}ProcessContent",
    f"{_MC}MustUnderstand",
)
# The local part of a qualified name (an XML NCName), or the wildcard of ``mc:ProcessContent``.
_QNAME_PART: Final = re.compile(r"\*|[^\W\d][\w.\-]*")
# The namespaces this extractor understands, in the sense of Markup Compatibility (ECMA-376 Part
# 3): an ``mc:Choice`` that requires only these is read, an element in one of them is never
# ignored (``mc:Ignorable`` applies only to namespaces a consumer does not understand), and
# ``mc:MustUnderstand`` may name only these. They are WordprocessingML itself; Word 2010's
# extensions to it (``w14``), whose elements that hold text are the co-authoring conflicts this
# module reads (the others are properties, which are left out, or markers); and the Word 2010
# drawing containers whose text is a text box's ``w:txbxContent``, which the paragraph walker
# reaches inside any drawing: a shape (``wps``), a group of shapes (``wpg``) and a drawing canvas
# (``wpc``, which holds shapes and groups). Their fallback may be only a picture, so reading it
# would lose the text. Every Word document declares ``w14`` ignorable, so not understanding it
# would drop the text of a co-authoring conflict. The other namespaces Word requires or declares
# ignorable, decided one by one: ink (``wpi``), Word 2010 drawing positions and sizes (``wp14``)
# and DrawingML extensions (``a14``) hold no text; Word's later extensions (``w15``, ``w16se`` and
# the other ``w16`` namespaces) hold properties, attributes and markers in the body, and where
# they hold a character in alternate content, the fallback carries it in plain WordprocessingML
# (an emoji that ``w16se:symEx`` names by code point is a ``w:t`` in the fallback); and a chart
# (``cx1`` to ``cx8``, chartex) keeps its text in a separate part, which is not read, like any
# other chart. A choice that requires anything else is skipped, and an element in any other
# namespace declared ignorable is dropped, as the standard asks of a consumer that does not
# understand it. An element in a namespace that is neither understood nor ignorable is read
# through, as any unknown wrapper is.
UNDERSTOOD: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        _W14[1:-1],
        "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
        "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
        "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    }
)
# Inside a paragraph every element is read through unless it is named here, so text in a container
# this module does not list (``w:smartTag``, ``w:hyperlink``, the base of ``w:ruby``, a wrapper a
# later Word adds) is kept rather than lost. Each of these is left out on purpose (see the module
# docstring).
EXCLUDED: Final = frozenset(
    {
        _PPR,  # paragraph properties
        _RPR,  # run properties
        _DEL,  # tracked deletion
        qn("w:moveFrom"),  # tracked move, the old place
        qn("w:delText"),  # deleted text
        qn("w:delInstrText"),  # deleted field code
        f"{_W14}conflictDel",  # deleted in a co-authoring conflict (Word 2010)
        qn("w:instrText"),  # field code
        qn("w:rt"),  # ruby pronunciation guide
        qn("w:sdtPr"),  # content control properties
        qn("w:sdtEndPr"),  # content control end properties
        qn("w:sym"),  # a glyph from a symbol font
        f"{_M}oMath",  # equation
        f"{_M}oMathPara",  # equation paragraph
    }
)
# The excluded elements whose text is left out because it is not shown, though it is part of the
# document as accepted: their field markers are still followed (as in a hidden run), so a field
# that opens or closes inside one does not leave the field state wrong for the text after it.
FIELDS_ONLY: Final = frozenset({qn("w:rt"), f"{_M}oMath", f"{_M}oMathPara"})
# Run content that stands for a character.
CHARACTERS: Final = {
    qn("w:tab"): "\t",
    qn("w:ptab"): "\t",
    qn("w:br"): "\n",
    qn("w:cr"): "\n",
    qn("w:noBreakHyphen"): "-",
}
_OFF: Final = frozenset({"0", "false", "off"})
_HEADING_LEVELS: Final = {"title": 1, **{f"heading {n}": n for n in range(1, 10)}}
_LINE_BREAK: Final = re.compile(r"\r\n?")
_CELL_SPACE: Final = re.compile(r"[\t\n]+")


class ImportedContentError(ExtractionError):
    """The DOCX shows content imported from another file (``w:altChunk``: HTML, RTF or another
    document, stored as a separate part that Word converts when it opens the file). This extractor
    does not read those parts, and a text without them would be incomplete, so the file is refused
    rather than reported as read."""

    def __init__(self) -> None:
        super().__init__("The DOCX holds content imported from another file, which is not read.")


class CompatibilityError(ExtractionError):
    """The DOCX uses Markup Compatibility (ECMA-376 Part 3) in a way this extractor cannot apply:
    ``mc:MustUnderstand`` names a namespace it does not understand, or the compatibility markup is
    an error under the standard. Reading on would give a text the producer said was not the
    document's, so the file is refused."""

    def __init__(self) -> None:
        super().__init__("The DOCX uses markup compatibility rules this extractor cannot apply.")


class NotDocxError(ExtractionError):
    """The blob is not a DOCX this extractor can read. ``reason`` is the validator's reason when its
    DOCX rule refused the bytes, and None when python-docx could not read a part."""

    def __init__(self, reason: Reason | None = None) -> None:
        self.reason = reason
        detail = reason.value if reason is not None else "unreadable_part"
        super().__init__(f"The file is not a readable DOCX ({detail}).")


def extract(data: bytes, limits: Limits = DEFAULT_LIMITS) -> Extraction:
    """One page holding the text of the DOCX in ``data``, with its headings."""
    reason = check_docx(data, limits)
    if reason is not None:
        raise NotDocxError(reason)
    bounded = _bounded_copy(data)
    try:
        document = Document(io.BytesIO(bounded))
    except MemoryError:
        raise
    except Exception:
        # Any failure of the library on these bytes is a document it cannot read; its message may
        # quote the file, so it is not kept. The library itself was imported with this module.
        raise NotDocxError from None
    del bounded
    # A Strict Open XML document (or any other namespace) passes the content type check but has no
    # element this module knows: refuse it instead of returning an empty text.
    root = document.element
    if root.tag != _DOCUMENT:
        raise NotDocxError
    # Every part read is seen as a consumer that understands ``UNDERSTOOD`` sees it.
    styles = document.styles.element
    for part in (root, styles):
        _apply_compatibility(part, _scope_of(part, _Scope()))
    body = root.find(_BODY)
    if body is None:
        raise NotDocxError
    levels = _heading_styles(styles)
    parts: list[str] = []
    headings: list[Heading] = []
    position = 0
    for text, level in _segments(body, levels, top=True):
        if not text.strip():
            continue
        if parts:
            position += len(PARAGRAPH_BREAK)
        if level is not None:
            headings.append(Heading(level, " ".join(text.split()), position, position + len(text)))
        parts.append(text)
        position += len(text)
    return Extraction(VERSION, (Page(1, PARAGRAPH_BREAK.join(parts), tuple(headings)),))


def _bounded_copy(data: bytes) -> bytes:
    """An uncompressed zip with every member of ``data``, each read no further than its claimed
    size. ``data`` has passed ``check_docx``, so the claims are within the limits."""
    out = io.BytesIO()
    try:
        with (
            zipfile.ZipFile(io.BytesIO(data)) as archive,
            zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as copy,
        ):
            for info in archive.infolist():
                if info.is_dir():
                    continue
                copy.writestr(info.filename, _read_member(archive, info))
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError, NotImplementedError):
        raise NotDocxError(Reason.UNREADABLE_ARCHIVE) from None
    return out.getvalue()


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """The member's bytes, read in pieces of ``READ_CHUNK``. ``zipfile`` stops at the claimed size
    and checks the CRC there; the count below holds the bound even if it did not."""
    pieces: list[bytes] = []
    total = 0
    with archive.open(info) as member:
        while piece := member.read(READ_CHUNK):
            total += len(piece)
            if total > info.file_size:
                raise NotDocxError(Reason.UNSAFE_ARCHIVE)
            pieces.append(piece)
    return b"".join(pieces)


def _heading_styles(styles: BaseOxmlElement) -> dict[str, int]:
    """Paragraph style id to heading level, for the built-in heading and title styles."""
    levels: dict[str, int] = {}
    for style in styles.iterchildren(_STYLE):
        name = style.find(_NAME)
        if style.get(_TYPE) != "paragraph" or name is None:
            continue
        level = _HEADING_LEVELS.get((name.get(_VAL) or "").strip().lower())
        style_id = style.get(_STYLE_ID)
        if level is not None and style_id is not None:
            levels[style_id] = level
    return levels


def _segments(
    container: BaseOxmlElement, levels: dict[str, int], *, top: bool
) -> Iterator[tuple[str, int | None]]:
    """Each paragraph and table under ``container`` as (text, heading level or None), in order.
    Headings are recognised only at the top of the body (``top``)."""
    for block in _blocks(container, (_P, _TBL)):
        if block.tag == _TBL:
            yield _table_text(block, levels), None
            continue
        inline = _Inline()
        inline.walk(block)
        text = _LINE_BREAK.sub("\n", "".join(inline.parts))
        yield text, _heading_level(block, levels) if top else None
        for box in inline.boxes:
            yield from _segments(box, levels, top=False)


def _blocks(container: BaseOxmlElement, wanted: tuple[str, ...]) -> Iterator[BaseOxmlElement]:
    """The children of ``container`` with a ``wanted`` tag, looking through content controls and
    custom XML wrappers. Alternate content is already resolved (``_apply_compatibility``)."""
    for child in container.iterchildren():
        if child.tag in wanted:
            yield child
        elif child.tag == _ALT_CHUNK:
            raise ImportedContentError
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                yield from _blocks(content, wanted)
        elif child.tag == _CUSTOM_XML:
            yield from _blocks(child, wanted)


@dataclass(frozen=True)
class _Scope:
    """The markup compatibility rules in force at an element: the namespaces declared ignorable
    there or on an ancestor, and for each ignorable namespace the element names whose content is
    processed (``*`` for all), from the nearest element that declares any for that namespace."""

    ignorable: frozenset[str] = frozenset()
    process: Mapping[str, frozenset[str]] = field(default_factory=dict)


def _apply_compatibility(element: BaseOxmlElement, scope: _Scope) -> None:
    """Apply Markup Compatibility (ECMA-376 Part 3) to the content of ``element``, in place, the way
    a consumer that understands only ``UNDERSTOOD`` must, so that every reader after it sees the
    document as such a consumer would:

    - ``mc:AlternateContent`` is replaced by the content of the representation ``_selected`` picks
      (nothing if none), so no other representation is ever read.
    - An element whose namespace is ignorable in its scope (``mc:Ignorable`` on it or an ancestor,
      its prefixes resolved where declared) and not understood is removed with everything in it,
      unless ``mc:ProcessContent`` in scope names it (or ``prefix:*``): then it is replaced by its
      content, which is processed under the element's own scope first.
    - ``mc:MustUnderstand`` naming a namespace not understood is refused, and so is compatibility
      markup the standard calls an error: an undeclared prefix, a ``ProcessContent`` entry that is
      not ``prefix:name`` or whose namespace is not declared ignorable on the same element, and a
      Markup Compatibility element outside its place (a choice outside alternate content, or an
      element the standard does not define).

    Every element's own compatibility attributes apply to the element itself and its content, and
    each subtree is processed before it moves, because prefixes in attribute values are resolved
    in the place where they are written. A representation of alternate content that is not read is
    not looked at, as the reference reader (.NET's ``XmlCompatibilityReader``) skips it."""
    for child in list(element.iterchildren()):
        tag = child.tag
        if not isinstance(tag, str):  # a comment or processing instruction
            continue
        inner = _scope_of(child, scope)
        namespace = tag[1:].partition("}")[0] if tag.startswith("{") else ""
        if namespace == _MC_NAMESPACE:
            if tag != _ALTERNATE:
                raise CompatibilityError
            chosen = _selected(child)
            if chosen is None:
                element.remove(child)
                continue
            _apply_compatibility(chosen, _scope_of(chosen, inner))
            _unwrap(element, child, chosen)
        elif namespace not in UNDERSTOOD and namespace in inner.ignorable:
            names = inner.process.get(namespace, frozenset())
            if "*" in names or tag.partition("}")[2] in names:
                _apply_compatibility(child, inner)
                _unwrap(element, child, child)
            else:
                element.remove(child)
        else:
            _apply_compatibility(child, inner)


def _unwrap(parent: BaseOxmlElement, child: BaseOxmlElement, content: BaseOxmlElement) -> None:
    """Put the children of ``content`` in the place of ``child`` (a child of ``parent``)."""
    position = parent.index(child)
    for offset, moved in enumerate(list(content.iterchildren())):
        parent.insert(position + offset, moved)
    parent.remove(child)


def _scope_of(element: BaseOxmlElement, outer: _Scope) -> _Scope:
    """The scope inside ``element``: ``outer`` with the element's own compatibility attributes."""
    in_scope = element.nsmap
    must = element.get(_MUST_UNDERSTAND)
    if must is not None and not set(_namespaces(must, in_scope)) <= UNDERSTOOD:
        raise CompatibilityError
    declared = element.get(_IGNORABLE)
    own = frozenset(_namespaces(declared, in_scope)) if declared is not None else frozenset()
    contents = element.get(_PROCESS_CONTENT)
    if not own and contents is None:
        return outer
    process = dict(outer.process)
    if contents is not None:
        named: dict[str, set[str]] = {}
        for entry in contents.split():
            prefix, _, name = entry.partition(":")
            namespace = in_scope.get(prefix) if prefix else None
            if namespace is None or namespace not in own or not _QNAME_PART.fullmatch(name):
                raise CompatibilityError
            named.setdefault(namespace, set()).add(name)
        process.update((namespace, frozenset(names)) for namespace, names in named.items())
    return _Scope(outer.ignorable | own, process)


def _namespaces(prefixes: str, in_scope: Mapping[str | None, str]) -> Iterator[str]:
    for prefix in prefixes.split():
        namespace = in_scope.get(prefix)
        if namespace is None:
            raise CompatibilityError
        yield namespace


def _selected(alternate: BaseOxmlElement) -> BaseOxmlElement | None:
    """The one representation of ``mc:AlternateContent`` that is read: the first ``mc:Choice``
    whose ``Requires`` names only namespaces in ``UNDERSTOOD``, or else the ``mc:Fallback``, or
    else nothing. Word writes some content twice (a text box as DrawingML and again as VML for old
    readers), so reading more than one would double it."""
    choice: BaseOxmlElement
    for choice in alternate.iterchildren(_CHOICE):
        if _understood(choice):
            return choice
    fallback: BaseOxmlElement | None = alternate.find(_FALLBACK)
    return fallback


def _understood(choice: BaseOxmlElement) -> bool:
    """Whether every prefix in the choice's ``Requires`` (a list separated by white space) names an
    understood namespace in scope there. The standard makes a missing or empty ``Requires``, and a
    prefix with no namespace in scope, an error, so the file is refused (``CompatibilityError``),
    as the reference reader does for each choice it looks at."""
    namespaces = list(_namespaces(choice.get("Requires") or "", choice.nsmap))
    if not namespaces:
        raise CompatibilityError
    return set(namespaces) <= UNDERSTOOD


def _table_text(table: BaseOxmlElement, levels: dict[str, int]) -> str:
    rows: list[str] = []
    for row in _blocks(table, (_TR,)):
        properties = row.find(_TRPR)
        if properties is not None and properties.find(_DEL) is not None:
            continue
        cells = [
            _CELL_SPACE.sub(
                " ", " ".join(t for t, _ in _segments(cell, levels, top=False) if t.strip())
            ).strip()
            for cell in _blocks(row, (_TC,))
            if not _cell_deleted(cell)
        ]
        if any(cells):
            rows.append(CELL_SEPARATOR.join(cells))
    return ROW_SEPARATOR.join(rows)


def _cell_deleted(cell: BaseOxmlElement) -> bool:
    """A cell deleted as a tracked change (``w:tcPr/w:cellDel``) no longer exists once the change
    is accepted, so its text is not read and the cells after it move up a column. An inserted cell
    (``w:cellIns``) is kept, like inserted text."""
    properties = cell.find(_TCPR)
    return properties is not None and properties.find(_CELL_DEL) is not None


class _Inline:
    """The text of one paragraph as accepted and shown, and the text boxes it anchors.

    A complex field's code runs from ``begin`` to ``separate`` and its result, the part shown, from
    ``separate`` to ``end``; text is kept only outside every open field's code, so a field nested in
    another field's code is left out too. The field state is kept per paragraph. Recursion is
    bounded: libxml2 refuses a document nested deeper than 256 elements.

    ``shown`` is False inside content that is in the document but not shown (a hidden run, an
    element of ``FIELDS_ONLY``): no text or text box is taken from it, but its field markers still
    change the state, because they delimit the code and result of the text that is shown.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.boxes: list[BaseOxmlElement] = []
        self._fields: list[bool] = []  # one per open field: True while in its code

    def walk(self, element: BaseOxmlElement, *, shown: bool = True) -> None:
        for child in element.iterchildren():
            tag = child.tag
            if tag in EXCLUDED:
                if tag in FIELDS_ONLY:
                    self.walk(child, shown=False)
                continue
            if tag == _T:
                self._add(child.text or "", shown)
            elif tag in CHARACTERS:
                self._add(CHARACTERS[tag], shown)
            elif tag == _FLD_CHAR:
                self._field(child.get(_FLD_CHAR_TYPE))
            elif tag == _TXBX_CONTENT:
                if shown:
                    self.boxes.append(child)
            elif tag == _ALT_CHUNK:  # block content, but refused wherever it is met
                raise ImportedContentError
            else:
                self.walk(child, shown=shown and not (tag == _R and _hidden(child)))

    def _add(self, text: str, shown: bool) -> None:
        if shown and not any(self._fields):
            self.parts.append(text)

    def _field(self, kind: str | None) -> None:
        if kind == "begin":
            self._fields.append(True)
        elif kind == "separate" and self._fields:
            self._fields[-1] = False
        elif kind == "end" and self._fields:
            self._fields.pop()


def _hidden(run: BaseOxmlElement) -> bool:
    properties = run.find(_RPR)
    vanish = properties.find(_VANISH) if properties is not None else None
    return vanish is not None and (vanish.get(_VAL) or "true").lower() not in _OFF


def _heading_level(paragraph: BaseOxmlElement, levels: dict[str, int]) -> int | None:
    properties = paragraph.find(_PPR)
    style = properties.find(_PSTYLE) if properties is not None else None
    if style is None:
        return None
    return levels.get(style.get(_VAL) or "")
