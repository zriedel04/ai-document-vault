"""Extractors turn a stored blob into pages of plain text with their structure (plan, section 7).

Each format has a module of its own (``text`` for TXT and Markdown so far; PDF and DOCX follow).
What an extractor returns is derived data: it can be rebuilt from the blob at any time, so the
result records the extractor's version, and a new version re-queues extraction (hash chain).

Offsets are positions in a page's ``text`` (Python string indices, so characters, not bytes). The
chunker and the citation highlight both work from them, so they must point at the text as stored.

Errors never name the file or quote its content: the job queue keeps only an exception's class name,
and the classes here say what went wrong in fixed words.
"""

from __future__ import annotations

from dataclasses import dataclass


class ExtractionError(Exception):
    """Base class for a blob an extractor cannot read. Messages are fixed text."""


class NotTextError(ExtractionError):
    """The file was accepted as text but its whole content is not (validation reads the head)."""

    def __init__(self) -> None:
        super().__init__("The file does not decode to text.")


@dataclass(frozen=True)
class Heading:
    """A heading in a page: its level (1 is the top), its title, and where it stands in the text.

    ``start`` and ``end`` span the whole heading line (or lines, for a Markdown heading underlined
    with ``===`` or ``---``), so ``text[start:end]`` is the heading as written.
    """

    level: int
    title: str
    start: int
    end: int


@dataclass(frozen=True)
class Page:
    """One page of a document. ``number`` counts from 1."""

    number: int
    text: str
    headings: tuple[Heading, ...] = ()


@dataclass(frozen=True)
class Extraction:
    """What an extractor read from one blob.

    ``extractor`` names the extractor and its version (``text-1``): the stage's input hash includes
    it. ``encoding`` is the character encoding the bytes were decoded with, when the format has one.
    """

    extractor: str
    pages: tuple[Page, ...]
    encoding: str | None = None
