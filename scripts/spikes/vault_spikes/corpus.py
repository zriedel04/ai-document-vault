"""Read the public synthetic corpus and its eval questions, and cut documents into chunks.

The corpus (``demo/corpus/``) and the questions (``eval/public/questions.yaml``) are synthetic, so
their text may be read and scored. A private folder passed to the same functions is treated the
same way, but the scripts never print or store any of its text.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import posixpath
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from vault_spikes.errors import OwnMessage, OwnValueError

TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv"})
MAX_FILE_BYTES = 20 * 1024 * 1024
"""Skip anything bigger: a spike input is a small file, and a zip can expand enormously."""
_WORD_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_XML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}
_ENTITY = re.compile("|".join(_XML_ENTITIES))


class SpikeInputError(OwnMessage, ValueError):
    """An input file is missing, malformed or points outside its folder."""


@dataclass(frozen=True)
class Source:
    """One passage of one document that an answer needs: where it is and what it says."""

    path: str
    quote: str


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    category: str
    answerable: bool
    sources: tuple[Source, ...]
    alternatives: tuple[Source, ...]
    # Set by the eval file's ``find_document: true``. Only these questions are scored by Hit@1,
    # which the architecture plan defines for "find the document" questions; it is never guessed
    # from the category or from how many documents the evidence happens to lie in.
    find_document: bool = False

    @property
    def source_paths(self) -> tuple[str, ...]:
        return tuple(source.path for source in self.sources)


@dataclass(frozen=True)
class Document:
    path: str
    kind: str
    pages: int | None
    needs_ocr: bool
    duplicate_of: str | None


@dataclass(frozen=True)
class Chunk:
    doc_path: str
    page: int
    text: str


def decode_text(data: bytes, errors: str = "strict") -> str:
    """Text from the bytes of a file: UTF-8 with or without a byte order mark, or UTF-16 or UTF-32
    with one.

    Windows tools save text in more than one way: Windows PowerShell 5.1 writes UTF-16 with ``>``
    and ``Out-File``, UTF-32 with ``-Encoding UTF32``, and puts a byte order mark in front of
    UTF-8 with ``Set-Content -Encoding UTF8``, and Notepad can save "UTF-8 with BOM". Each is read
    as the text it holds; a mark read as a character would stick to the first word or heading and
    lower a score without a warning, and would make a manifest invalid JSON. Line endings become
    ``\n``, as when Python reads a file as text.
    """
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):  # before UTF-16: FF FE 00 00
        text = data.decode("utf-32", errors)  # the mark gives the byte order and is removed
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = data.decode("utf-16", errors)
    else:
        text = data.decode("utf-8-sig", errors)  # removes a UTF-8 mark if there is one
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_owner_text(path: Path, what: str) -> str:
    """The text of a file the owner writes by hand (the manifest, the eval file, a reference text,
    a list of headings), decoded by :func:`decode_text`. Bytes that are not text in those encodings
    stop the run; the message names ``what``, fixed text of the caller, and never the file.

    UTF-16 or UTF-32 without a byte order mark is valid UTF-8 when the text is mostly ASCII, with a
    NUL between the letters, so a NUL, which no one types, stops the run too instead of becoming a
    reference that matches nothing.
    """
    try:
        text: str | None = decode_text(path.read_bytes())
    except UnicodeDecodeError:
        text = None
    if text is None or "\x00" in text:
        msg = f"{what} is not text this script can read; save it as UTF-8"
        raise SpikeInputError(msg)
    return text


def files_below(root: Path, suffixes: frozenset[str]) -> list[Path]:
    """Every file in ``root`` and its subfolders whose suffix (in any case) is in ``suffixes``,
    sorted by path, so one run can take a folder tree such as ``demo/corpus/``.

    A link anywhere in the tree stops the run (:func:`is_link`): a symbolic link to a folder or a
    file, a Windows junction (which ``os.walk`` enters, since it is not a symbolic link) or another
    reparse point. Followed, one could take the walk outside the tree, so a run on a folder inside
    ``demo/corpus/`` would report someone's own files as public data, or round a loop; skipped,
    its files would leave the measurement unseen. A folder that cannot be listed stops the run
    too, for the same reason (``os.walk`` would skip it without a word).
    """

    def fail(error: OSError) -> None:
        raise error

    found: list[Path] = []
    for folder, subfolders, names in os.walk(root, onerror=fail):
        for name in (*subfolders, *names):
            if is_link(Path(folder) / name):
                msg = "the folder holds a symbolic link or junction; the spike follows none"
                raise SpikeInputError(msg)
        subfolders.sort()
        found.extend(
            path
            for path in (Path(folder) / name for name in names)
            if path.suffix.lower() in suffixes and path.is_file()
        )
    return sorted(found)


_REPARSE_POINT: Final = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def is_link(path: Path) -> bool:
    """Is ``path`` itself a link: a symbolic link, or on Windows a junction or any other reparse
    point (``os.lstat`` does not follow it)? Junctions have the reparse attribute, not the
    symbolic-link mode, so ``Path.is_symlink`` misses them."""
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def reference_folder(file: Path, root: Path, references: Path | None) -> Path:
    """Where the reference files of ``file``, found by :func:`files_below` under ``root``, are:
    next to it, or with ``references`` at the same path relative to that folder as ``file`` has
    to ``root`` (``root/receipts/a.jpg`` has ``references/receipts/a.txt``).

    A link below ``references`` stops the run, as one below ``root`` does in :func:`files_below`:
    the references are a tree of their own that no walk checks, and a linked folder in it would
    take the reference texts from outside while the run is labelled by the folder it names. Each
    reference file is checked with :func:`not_a_link` before it is read.
    """
    if references is None:
        return file.parent
    folder = references
    for part in file.parent.relative_to(root).parts:
        folder = not_a_link(folder / part)
    return folder


def not_a_link(path: Path) -> Path:
    """``path``, unless it is a link (:func:`is_link`), which stops the run. A path that does not
    exist is returned as it is."""
    if os.path.lexists(path) and is_link(path):
        msg = "a reference path is a symbolic link or junction; the spike follows none"
        raise SpikeInputError(msg)
    return path


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"{what} must be a mapping"
        raise SpikeInputError(msg)
    return value


def _sources(entries: object, what: str) -> tuple[Source, ...]:
    """Each entry needs a path and the verbatim quote that marks the passage (eval/README.md)."""
    if entries is None:
        return ()
    if not isinstance(entries, list):
        msg = f"{what} must be a list"
        raise SpikeInputError(msg)
    sources: list[Source] = []
    for entry in entries:
        row = _mapping(entry, what)
        path, quote = row.get("path"), row.get("quote")
        if not isinstance(path, str) or not path:
            msg = f"every entry of {what} needs a path"
            raise SpikeInputError(msg)
        if not isinstance(quote, str) or not quote.strip():
            msg = f"every entry of {what} needs a quote, because retrieval is scored by passage"
            raise SpikeInputError(msg)
        sources.append(Source(path, quote))
    return tuple(sources)


def load_questions(path: Path) -> list[Question]:
    """Read ``eval/public/questions.yaml`` (format in ``eval/README.md``)."""
    try:
        data = _mapping(yaml.safe_load(read_owner_text(path, "the eval file")), "the eval file")
    except (OSError, yaml.YAMLError) as error:
        msg = f"cannot read the eval file: {type(error).__name__}"
        raise SpikeInputError(msg) from None
    rows = data.get("questions")
    if not isinstance(rows, list) or not rows:
        msg = "the eval file has no questions list"
        raise SpikeInputError(msg)
    questions: list[Question] = []
    seen: set[str] = set()
    for raw in rows:
        row = _mapping(raw, "a question")
        text, qid, category = row.get("question"), row.get("id"), row.get("category")
        answerable = row.get("answerable")
        if not (isinstance(text, str) and isinstance(qid, str) and isinstance(category, str)):
            msg = "a question needs id, question and category as text"
            raise SpikeInputError(msg)
        if not (qid.strip() and text.strip() and category.strip()):
            # A blank question is encoded as an empty query, whose arbitrary ranking would enter
            # Recall and MRR; a blank id or category is no name at all.
            msg = "a question needs a non-empty id, question and category"
            raise SpikeInputError(msg)
        if qid in seen:
            # Scores are kept per id, so a repeated id would silently score two questions with
            # one ranking.
            msg = "two questions have the same id"
            raise SpikeInputError(msg)
        seen.add(qid)
        if not isinstance(answerable, bool):
            msg = "answerable must be true or false"
            raise SpikeInputError(msg)
        sources = _sources(row.get("sources"), "sources")
        if answerable and not sources:
            msg = "an answerable question needs at least one source"
            raise SpikeInputError(msg)
        find_document = row.get("find_document", False)
        if not isinstance(find_document, bool):
            msg = "find_document must be true or false"
            raise SpikeInputError(msg)
        if find_document and (not answerable or len({s.path for s in sources}) != 1):
            msg = "a find_document question must be answerable from exactly one document"
            raise SpikeInputError(msg)
        alternatives = _sources(row.get("alternatives"), "alternatives")
        if alternatives and len(sources) != 1:
            # An alternative is a whole answer in one passage (eval/README.md). It can stand for
            # the one required passage; it cannot stand for one of several, so a question with
            # several sources has no alternatives and each of its passages is judged on its own.
            msg = "only a question with exactly one source can have alternatives"
            raise SpikeInputError(msg)
        questions.append(
            Question(
                qid,
                text,
                category,
                answerable,
                sources,
                alternatives,
                find_document,
            )
        )
    return questions


def load_manifest(path: Path) -> list[Document]:
    """Read ``demo/corpus/manifest.json``."""
    try:
        data = _mapping(json.loads(read_owner_text(path, "the manifest")), "the manifest")
    except SpikeInputError:
        raise
    except (OSError, ValueError) as error:
        msg = f"cannot read the manifest: {type(error).__name__}"
        raise SpikeInputError(msg) from None
    rows = data.get("documents")
    if not isinstance(rows, list) or not rows:
        msg = "the manifest has no documents list"
        raise SpikeInputError(msg)
    documents: list[Document] = []
    listed: set[str] = set()
    for raw in rows:
        row = _mapping(raw, "a document")
        doc_path, kind = row.get("path"), row.get("kind")
        if not isinstance(doc_path, str) or not isinstance(kind, str):
            msg = "a document needs a path and a kind"
            raise SpikeInputError(msg)
        # One file listed twice would be chunked twice, and its copies would take several of the
        # ten ranked places. Spellings Windows treats as one file count as one: case, the kind of
        # slash, and a leading "./".
        identity = posixpath.normpath(doc_path.replace("\\", "/")).casefold()
        if identity in listed:
            msg = "the manifest lists a document twice"
            raise SpikeInputError(msg)
        listed.add(identity)
        documents.append(
            Document(
                doc_path,
                kind,
                _optional(row, "pages", int),
                _optional(row, "needs_ocr", bool) or False,
                _optional(row, "exact_duplicate_of", str),
            )
        )
    _check_duplicate_references(documents)
    return documents


def _optional[T](row: Mapping[str, object], key: str, kind: type[T]) -> T | None:
    """A field that may be absent or null, and otherwise must be exactly of ``kind``.

    A value of another kind is refused and never converted or ignored: ``bool("false")`` is
    ``True``, so a hand-typed ``"needs_ocr": "false"`` would skip a readable document, an
    ``exact_duplicate_of`` that is not text would leave a copy in the corpus twice, and ``True``
    is not a page count. The error names the field (a fixed word of this format), never the
    value.
    """
    value = row.get(key)
    if value is None:
        return None
    if type(value) is not kind or (isinstance(value, int) and value < 0):  # not even bool
        msg = f"the manifest field {key} has a value of the wrong kind"
        raise SpikeInputError(msg)
    return value


def _check_duplicate_references(documents: list[Document]) -> None:
    """Refuse an ``exact_duplicate_of`` that names nothing, itself, or a loop. The mark makes the
    spike drop the copy and score its questions against the original, so a mark that points
    nowhere would drop a document on nothing but a claim. (That the bytes really are the same is
    checked when the files are read, by ``build_corpus``.)"""
    listed = {doc.path: doc for doc in documents}
    for doc in documents:
        if doc.duplicate_of is None:
            continue
        if doc.duplicate_of == doc.path or doc.duplicate_of not in listed:
            msg = "a document is marked as a duplicate of one the manifest does not list"
            raise SpikeInputError(msg)
        seen = {doc.path}
        current = doc
        while (target := current.duplicate_of) is not None:
            if target in seen:
                msg = "the manifest's exact duplicates form a loop"
                raise SpikeInputError(msg)
            seen.add(target)
            current = listed[target]


def check_sources_listed(questions: Sequence[Question], documents: Sequence[Document]) -> None:
    """Refuse a question that cites a path the manifest does not list, before the model runs.

    A misspelled or unlisted path would otherwise make the question unscorable, and it would be
    left out of Recall and MRR and counted with the questions that need an unreadable scan,
    where a data error reads as a limit of the reader. The spelling must be the manifest's own,
    not one Windows would treat as the same file: scoring compares the paths as written, so a
    path that differs only in case or slash would load, never match a chunk and score as a miss.
    The error gives the question's position in the eval file, a number this code counted: its id
    is free text of the owner's file (it may carry a client name) and so is the path.
    """
    listed = {doc.path for doc in documents}
    for number, question in enumerate(questions, start=1):
        if any(
            passage.path not in listed for passage in (*question.sources, *question.alternatives)
        ):
            msg = f"question number {number} of the eval file cites a document the manifest lacks"
            raise SpikeInputError(msg)


def file_digest(path: Path) -> str:
    """SHA-256 of a file's bytes, read in pieces (a listed file may be larger than is chunked)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def same_bytes(first: Path, second: Path) -> bool:
    """Do two files hold exactly the same bytes? The sizes are compared before any hashing."""
    return first.stat().st_size == second.stat().st_size and file_digest(first) == file_digest(
        second
    )


def canonical_paths(documents: list[Document]) -> dict[str, str]:
    """Map every document to the first of its exact duplicates, which are interchangeable.

    A duplicate that names a file the manifest does not list, or that is part of a cycle, is its
    own original: there is nothing else to stand in for it.
    """
    known = {doc.path: doc for doc in documents}
    canonical: dict[str, str] = {}
    for doc in documents:
        path, seen = doc.path, {doc.path}
        while (target := known[path].duplicate_of) is not None and target in known:
            if target in seen:
                path = doc.path
                break
            seen.add(target)
            path = target
        canonical[doc.path] = path
    return canonical


def safe_file(folder: Path, relative: str) -> Path:
    """``relative`` inside ``folder``, or SpikeInputError if it climbs out or is not a file."""
    root = folder.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        msg = "a listed document is missing or outside the corpus folder"
        raise SpikeInputError(msg)
    return candidate


def _docx_pages(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_FILE_BYTES:
            msg = "a document is too large to read"
            raise SpikeInputError(msg)
        xml = archive.read(info).decode("utf-8", errors="replace")
    paragraphs = []
    for paragraph in xml.split("</w:p>"):
        text = "".join(_WORD_TEXT.findall(paragraph))
        text = _ENTITY.sub(lambda m: _XML_ENTITIES[m.group(0)], text)
        if text.strip():
            paragraphs.append(text)
    return ["\n".join(paragraphs)]


PdfPages = Callable[[Path], list[str] | None]


def read_pages(path: Path, doc: Document, pdf_pages: PdfPages | None = None) -> list[str] | None:
    """The text of each page, or None if this script cannot read it (it is skipped and counted).

    Text files and DOCX are read directly. PDFs need ``pdf_pages`` (pypdfium2 on the PC), which
    returns None for a PDF it cannot read; pictures and scans need OCR, which is not what the
    embedding spike measures. A file that is corrupt, lacks its body, is too large, cannot be
    opened or is held by another program (an ``OSError`` at any step, the size check included) is
    skipped like any other unreadable file, so one bad input cannot stop the run.
    """
    if doc.needs_ocr:
        return None
    suffix = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        if suffix in TEXT_SUFFIXES:
            return [decode_text(path.read_bytes(), errors="replace")]
        if suffix == ".docx":
            return _docx_pages(path)
    except (zipfile.BadZipFile, KeyError, OSError, SpikeInputError):
        return None
    if suffix == ".pdf" and pdf_pages is not None:
        return pdf_pages(path)
    return None


DEFAULT_MAX_CHARS = 800
DEFAULT_OVERLAP = 100


def overlap_for(
    questions: Sequence[Question],
    max_chars: int = DEFAULT_MAX_CHARS,
    base: int = DEFAULT_OVERLAP,
) -> int:
    """The chunk overlap that keeps every evidence quote whole in some chunk.

    Two chunks that overlap by at least the length of a quote cannot both miss it: wherever a
    boundary falls, one chunk holds all of it. The longest quote (with a quarter more for the
    extra spaces and line breaks the page text may have) sets the overlap, never below ``base``
    and never above half a chunk. A quote longer than that is still reported by
    ``sources_missing_from`` when it falls across a boundary.
    """
    longest = max(
        (
            len(" ".join(source.quote.split()))
            for q in questions
            for source in (*q.sources, *q.alternatives)
        ),
        default=0,
    )
    return min(max(base, -(-longest * 5 // 4) + 1), max_chars // 2)


def split_text(
    text: str, max_chars: int = DEFAULT_MAX_CHARS, overlap: int = DEFAULT_OVERLAP
) -> list[str]:
    """Cut ``text`` into windows of at most ``max_chars``, ending at whitespace where possible."""
    if max_chars <= 0 or not 0 <= overlap < max_chars:
        msg = "need max_chars > 0 and 0 <= overlap < max_chars"
        raise OwnValueError(msg)
    text = text.strip()
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + max_chars // 2, end)
            end = cut if cut > start else end
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return pieces


@dataclass(frozen=True)
class Corpus:
    chunks: list[Chunk]
    readable: frozenset[str]
    skipped: int


def build_corpus(
    corpus_dir: Path,
    documents: list[Document],
    pdf_pages: PdfPages | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> Corpus:
    """Chunk every readable document. ``skipped`` counts the ones that could not be read.

    An exact duplicate is not chunked: the import pipeline stops at an exact-duplicate match, and
    two identical copies of a passage would take two of the ten places that Recall@10 and MRR
    score. It is ``readable`` (a question may cite it) when its original is, and scoring treats
    the two as one document (``canonical_paths``).
    """
    canonical = canonical_paths(documents)
    chunks: list[Chunk] = []
    readable: set[str] = set()
    skipped = 0
    for doc in documents:
        if canonical[doc.path] != doc.path:
            continue
        pages = read_pages(safe_file(corpus_dir, doc.path), doc, pdf_pages)
        if pages is None:
            skipped += 1
            continue
        readable.add(doc.path)
        for number, text in enumerate(pages, start=1):
            chunks.extend(
                Chunk(doc.path, number, piece) for piece in split_text(text, max_chars, overlap)
            )
    for doc in documents:
        if canonical[doc.path] != doc.path:
            # The mark is a claim about bytes, so the bytes are compared: a copy that is not one
            # would be dropped from the corpus and its questions scored against another file. It
            # is still a listed file, so a missing or linked one is refused like any other.
            copy = safe_file(corpus_dir, doc.path)
            original = safe_file(corpus_dir, canonical[doc.path])
            try:
                identical = same_bytes(copy, original)
            except OSError:
                # Bytes that cannot be read cannot be compared: the copy is left out and counted
                # like any unreadable file instead of stopping the run.
                skipped += 1
                continue
            if not identical:
                msg = "a document marked as an exact duplicate does not hold its original's bytes"
                raise SpikeInputError(msg)
            if canonical[doc.path] in readable:
                readable.add(doc.path)
            else:
                skipped += 1
    return Corpus(chunks, frozenset(readable), skipped)
