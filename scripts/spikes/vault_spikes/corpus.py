"""Read the public synthetic corpus and its eval questions, and cut documents into chunks.

The corpus (``demo/corpus/``) and the questions (``eval/public/questions.yaml``) are synthetic, so
their text may be read and scored. A private folder passed to the same functions is treated the
same way, but the scripts never print or store any of its text.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        data = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "the eval file")
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
        if not qid.strip():
            msg = "a question needs a non-empty id"
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
        questions.append(
            Question(
                qid,
                text,
                category,
                answerable,
                sources,
                _sources(row.get("alternatives"), "alternatives"),
                find_document,
            )
        )
    return questions


def load_manifest(path: Path) -> list[Document]:
    """Read ``demo/corpus/manifest.json``."""
    try:
        data = _mapping(json.loads(path.read_text(encoding="utf-8")), "the manifest")
    except (OSError, ValueError) as error:
        msg = f"cannot read the manifest: {type(error).__name__}"
        raise SpikeInputError(msg) from None
    rows = data.get("documents")
    if not isinstance(rows, list) or not rows:
        msg = "the manifest has no documents list"
        raise SpikeInputError(msg)
    documents: list[Document] = []
    for raw in rows:
        row = _mapping(raw, "a document")
        doc_path, kind = row.get("path"), row.get("kind")
        if not isinstance(doc_path, str) or not isinstance(kind, str):
            msg = "a document needs a path and a kind"
            raise SpikeInputError(msg)
        pages = row.get("pages")
        duplicate = row.get("exact_duplicate_of")
        documents.append(
            Document(
                doc_path,
                kind,
                pages if isinstance(pages, int) else None,
                bool(row.get("needs_ocr")),
                duplicate if isinstance(duplicate, str) else None,
            )
        )
    return documents


def canonical_paths(documents: list[Document]) -> dict[str, str]:
    """Map every document to the first of its exact duplicates, which are interchangeable."""
    return {doc.path: doc.duplicate_of or doc.path for doc in documents}


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
    embedding spike measures. A DOCX that is corrupt, lacks its body, is too large or cannot be
    opened is skipped like any other unreadable file, so one bad input cannot stop the run.
    """
    if doc.needs_ocr or path.stat().st_size > MAX_FILE_BYTES:
        return None
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return [path.read_text(encoding="utf-8", errors="replace")]
    if suffix == ".docx":
        try:
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
    """Chunk every readable document. ``skipped`` counts the ones that could not be read."""
    chunks: list[Chunk] = []
    readable: set[str] = set()
    skipped = 0
    for doc in documents:
        pages = read_pages(safe_file(corpus_dir, doc.path), doc, pdf_pages)
        if pages is None:
            skipped += 1
            continue
        readable.add(doc.path)
        for number, text in enumerate(pages, start=1):
            chunks.extend(
                Chunk(doc.path, number, piece) for piece in split_text(text, max_chars, overlap)
            )
    return Corpus(chunks, frozenset(readable), skipped)
