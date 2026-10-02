"""Score retrieved chunks against the eval questions by passage (Recall@10, MRR, Hit@1)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from vault_spikes.corpus import Chunk, Question, Source
from vault_spikes.metrics import cosine, mean

K = 10
# A quote that a chunk boundary cuts in two is matched by either half, but only for a quote long
# enough that half of it still identifies the passage.
SPLIT_QUOTE_MIN_WORDS = 8


@dataclass(frozen=True)
class Scores:
    questions: int
    recall_at_10: float
    mrr_at_10: float
    hit_at_1: float


def _normalise(text: str) -> str:
    return " ".join(text.split())


def covers(chunk: Chunk, source: Source, canonical: Mapping[str, str]) -> bool:
    """Does this chunk hold the passage the source names?

    The chunk must come from the same document (exact duplicates count as one document) and its
    text must contain the source's quote, with whitespace and line breaks ignored. A long quote
    that a chunk boundary splits is matched by the chunk that holds either half of it. Which page
    the quote is on does not matter: the quote itself identifies the passage.
    """
    if canonical.get(chunk.doc_path, chunk.doc_path) != canonical.get(source.path, source.path):
        return False
    text = _normalise(chunk.text)
    words = _normalise(source.quote).split()
    if not words:
        return False
    if " ".join(words) in text:
        return True
    if len(words) < SPLIT_QUOTE_MIN_WORDS:
        return False
    half = (len(words) + 1) // 2
    return " ".join(words[:half]) in text or " ".join(words[-half:]) in text


def score_question(
    question: Question,
    ranked: Sequence[Chunk],
    canonical: Mapping[str, str],
    k: int = K,
) -> tuple[float, float, float]:
    """(recall, reciprocal rank, hit@1) for one question, judged by passage.

    ``ranked`` is the retrieved chunks, best first. Recall is the share of the required passages
    that some chunk of the top ``k`` holds; for a question with one required passage, finding an
    alternative passage counts as finding it. The reciprocal rank is that of the first chunk that
    holds a required or alternative passage. A chunk from the right document that holds some other
    passage scores nothing.
    """
    top = ranked[:k]
    required = question.sources
    acceptable = (*required, *question.alternatives)
    found = [s for s in required if any(covers(chunk, s, canonical) for chunk in top)]
    recall = len(found) / len(required)
    alternative_found = any(covers(c, a, canonical) for c in top for a in question.alternatives)
    if len(required) == 1 and not found and alternative_found:
        recall = 1.0
    rank = next(
        (
            position
            for position, chunk in enumerate(top, start=1)
            if any(covers(chunk, s, canonical) for s in acceptable)
        ),
        None,
    )
    return recall, (1 / rank if rank else 0.0), (1.0 if rank == 1 else 0.0)


def score_all(
    questions: Sequence[Question],
    ranked: Mapping[str, Sequence[Chunk]],
    canonical: Mapping[str, str],
    k: int = K,
) -> Scores:
    """Average the per-question scores over the questions that have a ranking."""
    results = [score_question(q, ranked[q.id], canonical, k) for q in questions if q.id in ranked]
    if not results:
        msg = "no question could be scored"
        raise ValueError(msg)
    return Scores(
        len(results),
        mean([r[0] for r in results]),
        mean([r[1] for r in results]),
        mean([r[2] for r in results]),
    )


def sources_missing_from(
    questions: Sequence[Question], chunks: Sequence[Chunk], canonical: Mapping[str, str]
) -> int:
    """How many required passages no chunk of the corpus holds at all.

    Such a passage can never be retrieved, whatever the model does (a quote that is mistyped, text
    the reader cannot extract, a passage cut in several places), so the count is reported next to
    the scores instead of silently lowering them.
    """
    return sum(
        1
        for q in questions
        for source in q.sources
        if not any(covers(chunk, source, canonical) for chunk in chunks)
    )


def rank_by_similarity(
    query_vector: Sequence[float],
    chunk_vectors: Sequence[Sequence[float]],
    chunks: Sequence[Chunk],
    k: int = K,
    similarity: Callable[[Sequence[float], Sequence[float]], float] = cosine,
) -> list[Chunk]:
    """The ``k`` chunks most similar to the query, best first."""
    scored = sorted(
        range(len(chunk_vectors)),
        key=lambda i: similarity(query_vector, chunk_vectors[i]),
        reverse=True,
    )
    return [chunks[i] for i in scored[:k]]
