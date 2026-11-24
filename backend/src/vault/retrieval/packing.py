"""Context packing for Ask the Vault (plan section 8, steps 6 and 7; section 11; ADR 0011).

A pure function, :func:`pack`, from a fused ranking (:mod:`vault.retrieval.fusion`, already capped
per document by the caller) and the chunks it names to the sources the answer model will see,
labelled ``S1`` to ``Sn``. Nothing calls it yet, and nothing here calls a model: the answer step
is a later change. The caller loads the chunks (each hit's chunk and, for expansion, the chunks
next to it) and the documents' titles; this module reads nothing and writes nothing.

The rules the plan leaves open:

- **Sources are runs of chunks.** Two chunks belong to one source when they are next to each
  other in one document version (``index`` and ``index + 1``) and have the same section path,
  whether they lie on one page or on two. A source's text is its chunks in reading order, with
  no text twice: where the next chunk starts again inside the previous one (the chunker's
  overlap, same page, ``char_start < previous char_end``) only the part after the previous chunk's
  end is added, which is the page's own text, and otherwise the two are joined by
  :data:`SEPARATOR` (only white space lies between two chunks on a page, and a page break between
  pages). Overlapping text that does not match is a :class:`PackingError`.
- **Packing is greedy in the order of the ranking.** Each hit, best first, joins the source it is
  next to (both, merged, if it closes the gap between two) or starts a new one, if the estimated
  size of all sources stays within ``[retrieval] context_budget_tokens`` (the plan's B); if not,
  that hit is left out and the next one is tried, so a smaller hit further down can still fit. A
  chunk larger than the budget on its own is always left out. Sources keep the place of the
  best hit that made them, and a merge keeps the better place.
- **Neighbours fill what is left.** Then, for up to ``context_neighbor_chunks`` rounds, each
  source in order adds the chunk before it and then the chunk after it, if the caller supplied
  that chunk, it has the same section path, and it fits. A neighbour is context, not a hit: it
  carries no score, does not count towards the per-document cap, and never displaces a hit,
  because every hit is placed first. (A hit left out for its size can still come in this way,
  when joining a source costs less than a source of its own; it then keeps its hit.)
- **Labels.** The sources are labelled ``S1``, ``S2`` ... in their order. Each holds what the
  prompt and the citation validator need: the document, version and title, the pages, the section
  path (empty when the text is under no heading), the text, and its parts, each part being the
  span of one chunk it took, with its place in the source text and on the page, so a quote found
  in the source text maps back to a chunk, page and characters (:meth:`PackedSource.parts_for`).
- **Order.** The output depends only on the ranking's order and the chunks' content, never on the
  order of the mappings passed in.

The token estimate (:func:`estimate_tokens`) is deliberately high, because the project has no
tokenizer and the budget must not be overrun: every run of up to three ASCII letters or digits is
a token, every other visible character is a token of its own, and so is every line break. Real
sub-word tokenizers average about four characters of English per token, so English is estimated
about a third high; accented letters, Chinese and Japanese count one token per character, at or
above what those tokenizers use. It is never below the chunker's own count
(``structural_v1.count_tokens``, which counts up to four letters or digits as one) and never above
the number of UTF-8 bytes, which bounds any byte-level tokenizer. A rare character (an emoji)
can still cost a real tokenizer more than one token, so B should stay below the model's limit
with room to spare. Each source is charged its text, its title and section titles, and a fixed
:data:`SOURCE_ALLOWANCE_TOKENS` for its label, pages and delimiters; the answer step must keep
its source markup within that allowance, or charge escaping that lengthens the text itself.

Chunk text, section titles and document titles are the owner's documents and file names: they are
never logged and no error quotes them. :class:`PackingError` messages are fixed text, and the one
log line is counts only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from vault.core.config import RetrievalSettings
from vault.core.logging import log_event, metric
from vault.db.ids import is_ulid
from vault.retrieval.fusion import FusedHit

logger = logging.getLogger(__name__)

# Joins two chunks of one source that do not overlap.
SEPARATOR: Final = "\n\n"
# What each source is charged on top of its text and titles: its label, page numbers, the
# delimiters of its block in the prompt and the separators between section titles.
SOURCE_ALLOWANCE_TOKENS: Final = 32

_ESTIMATE: Final = re.compile(r"[0-9A-Za-z]{1,3}|\n|\S")


class PackingError(ValueError):
    """An input packing cannot use. The message is fixed text and quotes no text or title."""


def estimate_tokens(text: str) -> int:
    """A conservative token count of ``text`` (see the module text for the rule and its bounds)."""
    return len(_ESTIMATE.findall(text))


def _whole(value: object, low: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= low


@dataclass(frozen=True)
class ContextChunk:
    """A stored chunk (``chunks`` row) as packing needs it, with ``section_path`` decoded."""

    chunk_id: int
    document_id: str
    version_id: str
    index: int
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    section_path: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        if not _whole(self.chunk_id, 1) or not _whole(self.index, 0):
            msg = "a chunk id must be a whole number from 1 and its index one from 0"
            raise PackingError(msg)
        if not is_ulid(self.document_id) or not is_ulid(self.version_id):
            msg = "a chunk's document and version ids must be ULIDs"
            raise PackingError(msg)
        if (
            not all(_whole(n, 1) for n in (self.page_start, self.page_end))
            or not all(_whole(n, 0) for n in (self.char_start, self.char_end))
            or self.page_end < self.page_start
            or (self.page_start == self.page_end and self.char_end <= self.char_start)
        ):
            msg = "a chunk's pages and offsets must be whole numbers in order"
            raise PackingError(msg)
        if not isinstance(self.text, str) or not self.text:
            msg = "a chunk's text must be text that is not empty"
            raise PackingError(msg)
        if self.page_start == self.page_end and len(self.text) != self.char_end - self.char_start:
            msg = "a chunk's text must be as long as its span"
            raise PackingError(msg)
        if not isinstance(self.section_path, tuple) or not all(
            isinstance(title, str) for title in self.section_path
        ):
            msg = "a chunk's section path must be a tuple of titles"
            raise PackingError(msg)


@dataclass(frozen=True)
class SourcePart:
    """The span of one chunk that a source holds.

    ``source_start``/``source_end`` are offsets in :attr:`PackedSource.text`; ``chunk_start`` is
    where the part begins in the chunk's own text (after any overlap with the chunk before it), and
    ``page_start``/``char_start`` to ``page_end``/``char_end`` is the same span on the pages, in
    the chunk's coordinates. ``hit`` is the fused hit that brought the chunk in, or ``None`` for a
    neighbour added for context.
    """

    chunk_id: int
    source_start: int
    source_end: int
    chunk_start: int
    page_start: int
    char_start: int
    page_end: int
    char_end: int
    hit: FusedHit | None


@dataclass(frozen=True)
class PackedSource:
    """One labelled source: the text the answer model sees as ``[label]``, and where it is from."""

    label: str
    document_id: str
    version_id: str
    title: str
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    text: str
    parts: tuple[SourcePart, ...]
    estimated_tokens: int

    def parts_for(self, start: int, end: int) -> tuple[SourcePart, ...]:
        """The parts that ``text[start:end]`` (a matched quote) touches, in reading order."""
        if not (_whole(start, 0) and _whole(end, 0) and start < end <= len(self.text)):
            msg = "a span must be a non-empty range inside the source text"
            raise PackingError(msg)
        return tuple(p for p in self.parts if p.source_start < end and start < p.source_end)


@dataclass(frozen=True)
class PackedContext:
    """The packed sources, S1 first, and what packing left out (counts only)."""

    sources: tuple[PackedSource, ...]
    budget_tokens: int
    estimated_tokens: int
    hits_packed: int
    hits_left_out: int
    neighbors_added: int

    def source(self, label: str) -> PackedSource | None:
        """The source with ``label`` (``"S3"``), or ``None`` if no source has it."""
        return next((s for s in self.sources if s.label == label), None)


@dataclass(eq=False)
class _Run:
    """A source while it is built: chunks of one version and section, consecutive by index."""

    place: int
    chunks: list[ContextChunk]
    hits: dict[int, FusedHit] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        return self.chunks[0].version_id, self.chunks[0].section_path

    @property
    def first(self) -> int:
        return self.chunks[0].index

    @property
    def last(self) -> int:
        return self.chunks[-1].index


def _assemble(chunks: Sequence[ContextChunk]) -> tuple[str, list[tuple[int, int, int, int, int]]]:
    """The text of consecutive chunks without repeats, and per chunk (source start, source end,
    chunk start, page, char) where its part begins."""
    pieces: list[str] = []
    spans: list[tuple[int, int, int, int, int]] = []
    length = 0
    previous: ContextChunk | None = None
    for chunk in chunks:
        skip = 0
        if previous is not None:
            if chunk.page_start < previous.page_end:
                msg = "consecutive chunks go back a page"
                raise PackingError(msg)
            if chunk.page_start == previous.page_end and chunk.char_start < previous.char_end:
                skip = previous.char_end - chunk.char_start
                if (
                    skip >= len(chunk.text)
                    or skip > len(previous.text)
                    or chunk.text[:skip] != previous.text[-skip:]
                ):
                    msg = "consecutive chunks overlap with different text"
                    raise PackingError(msg)
            else:
                pieces.append(SEPARATOR)
                length += len(SEPARATOR)
        part = chunk.text[skip:]
        start = length
        pieces.append(part)
        length += len(part)
        spans.append((start, length, skip, chunk.page_start, chunk.char_start + skip))
        previous = chunk
    return "".join(pieces), spans


def _cost(run_chunks: Sequence[ContextChunk], title: str) -> int:
    text, _ = _assemble(run_chunks)
    section = run_chunks[0].section_path
    return (
        SOURCE_ALLOWANCE_TOKENS
        + estimate_tokens(title)
        + sum(estimate_tokens(t) for t in section)
        + estimate_tokens(text)
    )


class _Packer:
    def __init__(self, titles: Mapping[str, str], budget: int) -> None:
        self.titles = titles
        self.budget = budget
        self.runs: list[_Run] = []
        self.costs: dict[int, int] = {}  # id(run) -> cost
        self.used = 0
        self.placed: set[int] = set()

    def add(self, chunk: ContextChunk, place: int, hit: FusedHit | None) -> bool:
        """Place ``chunk`` in a source if everything still fits; whether it was placed."""
        key = (chunk.version_id, chunk.section_path)
        touching = [
            run
            for run in self.runs
            if run.key == key and chunk.index in (run.first - 1, run.last + 1)
        ]
        merged = sorted((c for run in touching for c in run.chunks), key=lambda c: c.index)
        merged.append(chunk)
        merged.sort(key=lambda c: c.index)
        cost = _cost(merged, self.titles[chunk.document_id])
        used = self.used - sum(self.costs[id(run)] for run in touching) + cost
        if used > self.budget:
            return False
        new = _Run(min([place, *(run.place for run in touching)]), merged)
        for run in touching:
            new.hits.update(run.hits)
            self.runs.remove(run)
            del self.costs[id(run)]
        if hit is not None:
            new.hits[chunk.chunk_id] = hit
        self.runs.append(new)
        self.costs[id(new)] = cost
        self.used = used
        self.placed.add(chunk.chunk_id)
        return True


def _check_chunks(chunks: Mapping[int, ContextChunk]) -> dict[tuple[str, int], ContextChunk]:
    by_position: dict[tuple[str, int], ContextChunk] = {}
    documents: dict[str, str] = {}
    for chunk_id, chunk in chunks.items():
        if not isinstance(chunk, ContextChunk) or chunk_id != chunk.chunk_id:
            msg = "the chunks must be keyed by their own chunk ids"
            raise PackingError(msg)
        position = (chunk.version_id, chunk.index)
        if position in by_position:
            msg = "two chunks have the same version and index"
            raise PackingError(msg)
        by_position[position] = chunk
        if documents.setdefault(chunk.version_id, chunk.document_id) != chunk.document_id:
            msg = "a version's chunks name two different documents"
            raise PackingError(msg)
    return by_position


def _check_ranking(
    ranking: Iterable[FusedHit], chunks: Mapping[int, ContextChunk], titles: Mapping[str, str]
) -> tuple[FusedHit, ...]:
    checked = tuple(ranking)
    seen: set[int] = set()
    for hit in checked:
        if not isinstance(hit, FusedHit):
            msg = "a ranking must hold fused hits"
            raise PackingError(msg)
        if hit.chunk_id in seen:
            msg = "a ranking lists a chunk twice"
            raise PackingError(msg)
        seen.add(hit.chunk_id)
        chunk = chunks.get(hit.chunk_id)
        if chunk is None or chunk.document_id != hit.document_id:
            msg = "a hit's chunk is missing or belongs to another document"
            raise PackingError(msg)
        title = titles.get(hit.document_id)
        if not isinstance(title, str) or not title.strip():
            msg = "a hit's document has no title"
            raise PackingError(msg)
    return checked


def pack(
    ranking: Iterable[FusedHit],
    chunks: Mapping[int, ContextChunk],
    titles: Mapping[str, str],
    settings: RetrievalSettings,
) -> PackedContext:
    """The labelled sources for ``ranking`` within the budget (see the module text).

    ``chunks`` maps a chunk id to its chunk and must hold every hit's chunk; any other chunk it
    holds may be added as a neighbour. ``titles`` maps a document id to the document's name as the
    library shows it. Raises :class:`PackingError` (fixed text) for an input that breaks the rules.
    """
    by_position = _check_chunks(chunks)
    hits = _check_ranking(ranking, chunks, titles)
    packer = _Packer(titles, settings.context_budget_tokens)
    hit_of = {hit.chunk_id: hit for hit in hits}
    for place, hit in enumerate(hits):
        packer.add(chunks[hit.chunk_id], place, hit)

    for _ in range(settings.context_neighbor_chunks):
        for run in sorted(packer.runs, key=lambda r: r.place):
            if run not in packer.runs:  # merged into an earlier source this round
                continue
            current = run
            for step in (-1, 1):
                index = current.first - 1 if step < 0 else current.last + 1
                neighbor = by_position.get((current.key[0], index))
                if (
                    neighbor is None
                    or neighbor.chunk_id in packer.placed
                    or neighbor.section_path != current.key[1]
                ):
                    continue
                if packer.add(neighbor, current.place, hit_of.get(neighbor.chunk_id)):
                    current = next(r for r in packer.runs if neighbor in r.chunks)

    hits_packed = len(packer.placed & hit_of.keys())
    sources = []
    for number, run in enumerate(sorted(packer.runs, key=lambda r: r.place), start=1):
        text, spans = _assemble(run.chunks)
        parts = tuple(
            SourcePart(
                chunk_id=chunk.chunk_id,
                source_start=source_start,
                source_end=source_end,
                chunk_start=chunk_start,
                page_start=page,
                char_start=char,
                page_end=chunk.page_end,
                char_end=chunk.char_end,
                hit=run.hits.get(chunk.chunk_id),
            )
            for chunk, (source_start, source_end, chunk_start, page, char) in zip(
                run.chunks, spans, strict=True
            )
        )
        first = run.chunks[0]
        sources.append(
            PackedSource(
                label=f"S{number}",
                document_id=first.document_id,
                version_id=first.version_id,
                title=titles[first.document_id],
                page_start=first.page_start,
                page_end=run.chunks[-1].page_end,
                section_path=first.section_path,
                text=text,
                parts=parts,
                estimated_tokens=packer.costs[id(run)],
            )
        )
    context = PackedContext(
        sources=tuple(sources),
        budget_tokens=settings.context_budget_tokens,
        estimated_tokens=packer.used,
        hits_packed=hits_packed,
        hits_left_out=len(hits) - hits_packed,
        neighbors_added=len(packer.placed) - hits_packed,
    )
    log_event(
        logger,
        "retrieval.context_packed",
        level=logging.DEBUG,
        sources=metric(len(context.sources)),
        hits_packed=metric(context.hits_packed),
        hits_left_out=metric(context.hits_left_out),
        neighbors_added=metric(context.neighbors_added),
        estimated_tokens=metric(context.estimated_tokens),
        budget_tokens=metric(context.budget_tokens),
    )
    return context
