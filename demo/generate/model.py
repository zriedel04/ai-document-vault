"""Data model for the synthetic corpus. Everything here is fictional."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Style = Literal["h1", "h2", "p", "li"]
Kind = Literal["pdf_text", "pdf_scan", "docx", "txt", "md", "csv", "png", "jpg"]


@dataclass(frozen=True)
class Block:
    """One paragraph-like block of a PDF or DOCX page."""

    style: Style
    text: str


@dataclass(frozen=True)
class Document:
    """A document in the corpus.

    ``pages`` holds the ground-truth text of each page, one string per page. For pdf_text and docx
    it is built from ``blocks``; for scans and images it is the lines drawn into the picture; for
    txt, md and csv it is the file content. Formats without pages (docx, txt, md, csv, images) have
    a single entry, and the eval set refers to them with ``page: null``.
    """

    path: str
    kind: Kind
    blocks: tuple[tuple[Block, ...], ...] = ()  # per page, for pdf_text and docx
    lines: tuple[tuple[str, ...], ...] = ()  # per page, for scans and images
    text: str = ""  # for txt, md and csv
    layout: str = ""  # which image layout to draw (screenshots and receipts)
    exact_duplicate_of: str | None = None
    near_duplicate_of: str | None = None
    note: str = ""
    seed: int = 0
    extra: dict[str, str] = field(default_factory=dict)

    def page_texts(self) -> list[str]:
        if self.kind in ("txt", "md", "csv"):
            return [self.text.rstrip("\n")]
        if self.kind in ("pdf_text", "docx"):
            # PDFs draw a "- " before list items; DOCX uses real bullets, which have no text.
            dash = self.kind == "pdf_text"
            return ["\n".join(_block_text(b, dash) for b in page) for page in self.blocks]
        return ["\n".join(page) for page in self.lines]

    @property
    def paginated(self) -> bool:
        return self.kind in ("pdf_text", "pdf_scan")


def _block_text(block: Block, dash: bool) -> str:
    return f"- {block.text}" if dash and block.style == "li" else block.text


def h1(text: str) -> Block:
    return Block("h1", text)


def h2(text: str) -> Block:
    return Block("h2", text)


def p(text: str) -> Block:
    return Block("p", text)


def li(text: str) -> Block:
    return Block("li", text)
