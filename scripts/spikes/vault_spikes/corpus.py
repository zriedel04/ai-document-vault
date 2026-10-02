"""Read the public synthetic corpus and its eval questions, and cut documents into chunks.

The corpus (``demo/corpus/``) and the questions (``eval/public/questions.yaml``) are synthetic, so
their text may be read and scored. A private folder passed to the same functions is treated the
same way, but the scripts never print or store any of its text.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TEXT_SUFFIXES = frozenset({".txt", ".md", ".csv"})
MAX_FILE_BYTES = 20 * 1024 * 1024
"""Skip anything bigger: a spike input is a small file, and a zip can expand enormously."""
_WORD_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_XML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}
_ENTITY = re.compile("|".join(_XML_ENTITIES))


class SpikeInputError(ValueError):
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
    for raw in rows:
        row = _mapping(raw, "a question")
        text, qid, category = row.get("question"), row.get("id"), row.get("category")
        answerable = row.get("answerable")
        if not (isinstance(text, str) and isinstance(qid, str) and isinstance(category, str)):
            msg = "a question needs id, question and category as text"
            raise SpikeInputError(msg)
        if not isinstance(answerable, bool):
            msg = "answerable must be true or false"
            raise SpikeInputError(msg)
        sources = _sources(row.get("sources"), "sources")
        if answerable and not sources:
            msg = "an answerable question needs at least one source"
            raise SpikeInputError(msg)
        questions.append(
            Question(
                qid,
                text,
                category,
                answerable,
                sources,
                _sources(row.get("alternatives"), "alternatives"),
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


PdfPages = Callable[[Path], list[str]]


def read_pages(path: Path, doc: Document, pdf_pages: PdfPages | None = None) -> list[str] | None:
    """The text of each page, or None if this script cannot read it (it is skipped and counted).

    Text files and DOCX are read directly. PDFs need ``pdf_pages`` (pypdfium2 on the PC); pictures
    and scans need OCR, which is not what the embedding spike measures.
    """
    if doc.needs_ocr or path.stat().st_size > MAX_FILE_BYTES:
        return None
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return [path.read_text(encoding="utf-8", errors="replace")]
    if suffix == ".docx":
        return _docx_pages(path)
    if suffix == ".pdf" and pdf_pages is not None:
        return pdf_pages(path)
    return None


def split_text(text: str, max_chars: int = 800, overlap: int = 100) -> list[str]:
    """Cut ``text`` into windows of at most ``max_chars``, ending at whitespace where possible."""
    if max_chars <= 0 or not 0 <= overlap < max_chars:
        msg = "need max_chars > 0 and 0 <= overlap < max_chars"
        raise ValueError(msg)
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
    max_chars: int = 800,
    overlap: int = 100,
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
