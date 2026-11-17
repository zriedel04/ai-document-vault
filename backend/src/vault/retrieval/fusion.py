"""Reciprocal Rank Fusion of the keyword and vector rankings (plan section 8, steps 4 and 6).

ADR 0008 records the decision.

Pure functions on ranked lists of chunk ids, with every number from ``[retrieval]``
(:class:`vault.core.config.RetrievalSettings`). Nothing calls them yet: search is keyword only
until vectors exist (ADR 0013 is Proposed).

:func:`fuse` (step 4) reads the first ``keyword_candidates`` distinct chunks of the keyword list
and the first ``vector_candidates`` of the vector list (the plan's K_kw and K_vec), ranks each list
from 1, and scores every chunk in either as the sum over the lists it is in of
``1 / (rrf_k + rank)``. The rules the plan leaves open:

- **Repeats.** A chunk listed twice in one list keeps its first (best) place, and the repeat takes
  no place: ranks count distinct chunks, so a repeat neither pushes the chunks after it down nor
  uses up a candidate.
- **One chunk, one document.** A chunk is in exactly one document, so a chunk given two document
  ids (in one list or across the two) is a :class:`FusionError`, as is a hit that is not a
  :class:`Hit`. Every hit is checked, those past the candidate count too.
- **Exact scores.** Scores are fractions (:class:`fractions.Fraction`), not floats, so two chunks
  whose sums are equal are equal (``1/(k+1) + 1/(k+3)`` against ``1/(k+2) + 1/(k+2)`` is decided
  by arithmetic, not by rounding) and the order does not depend on the order of the additions.
- **Ties.** Higher score first; equal scores in ascending chunk id. The order is total, so the
  same input always gives the same ranking.

:func:`cap_per_document` (the per-document part of step 6) keeps, in fused order, at most
``max_chunks_per_document`` chunks of each document (the plan's M) and drops the rest; a chunk
listed twice is a :class:`FusionError`, since :func:`fuse` never lists one twice. The plan
lifts the cap when the scope is a single document; that is the caller's choice, by not calling it.

The boosts the plan names (title or label match, recency) are not here yet: what counts as a match
and how recency is measured are open, and a boost needs inputs a ranked list of ids does not hold.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction

from vault.core.config import RetrievalSettings
from vault.db.ids import is_ulid


class FusionError(ValueError):
    """An input fusion cannot rank. The message is fixed text and names no id."""


@dataclass(frozen=True)
class Hit:
    """One place in a ranked list: a chunk (``chunks.id``) and the document it belongs to."""

    chunk_id: int
    document_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.chunk_id, int)
            or isinstance(self.chunk_id, bool)
            or self.chunk_id < 1
        ):
            msg = "a chunk id must be a whole number from 1"
            raise FusionError(msg)
        if not is_ulid(self.document_id):
            msg = "a document id must be a ULID"
            raise FusionError(msg)


@dataclass(frozen=True)
class FusedHit:
    """A chunk in the fused ranking: its exact score and its rank in each list (None if absent)."""

    chunk_id: int
    document_id: str
    score: Fraction
    keyword_rank: int | None
    vector_rank: int | None


def _ranks(hits: Iterable[Hit], limit: int, documents: dict[int, str]) -> dict[int, int]:
    """Rank the first ``limit`` distinct chunks of ``hits`` from 1, checking every hit."""
    ranks: dict[int, int] = {}
    seen: set[int] = set()
    for hit in hits:
        if not isinstance(hit, Hit):
            msg = "a ranked list must hold hits"
            raise FusionError(msg)
        known = documents.setdefault(hit.chunk_id, hit.document_id)
        if known != hit.document_id:
            msg = "a chunk is listed with two different documents"
            raise FusionError(msg)
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        if len(ranks) < limit:
            ranks[hit.chunk_id] = len(ranks) + 1
    return ranks


def fuse(
    keyword: Iterable[Hit], vector: Iterable[Hit], settings: RetrievalSettings
) -> tuple[FusedHit, ...]:
    """The fused ranking of a keyword and a vector ranking, best first (see the module text)."""
    documents: dict[int, str] = {}
    keyword_ranks = _ranks(keyword, settings.keyword_candidates, documents)
    vector_ranks = _ranks(vector, settings.vector_candidates, documents)
    fused = []
    for chunk_id in keyword_ranks.keys() | vector_ranks.keys():
        keyword_rank = keyword_ranks.get(chunk_id)
        vector_rank = vector_ranks.get(chunk_id)
        score = sum(
            (
                Fraction(1, settings.rrf_k + rank)
                for rank in (keyword_rank, vector_rank)
                if rank is not None
            ),
            Fraction(0),
        )
        fused.append(FusedHit(chunk_id, documents[chunk_id], score, keyword_rank, vector_rank))
    fused.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    return tuple(fused)


def cap_per_document(
    ranking: Sequence[FusedHit], settings: RetrievalSettings
) -> tuple[FusedHit, ...]:
    """``ranking`` in order, with at most ``max_chunks_per_document`` chunks of each document."""
    kept: list[FusedHit] = []
    per_document: dict[str, int] = {}
    seen: set[int] = set()
    for hit in ranking:
        if not isinstance(hit, FusedHit):
            msg = "a fused ranking must hold fused hits"
            raise FusionError(msg)
        if hit.chunk_id in seen:
            msg = "a fused ranking lists a chunk twice"
            raise FusionError(msg)
        seen.add(hit.chunk_id)
        count = per_document.get(hit.document_id, 0)
        if count < settings.max_chunks_per_document:
            per_document[hit.document_id] = count + 1
            kept.append(hit)
    return tuple(kept)
