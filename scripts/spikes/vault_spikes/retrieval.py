"""Score a ranked list of retrieved chunks against the eval questions (Recall@10, MRR, Hit@1)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from vault_spikes.corpus import Question
from vault_spikes.metrics import cosine, mean

K = 10


@dataclass(frozen=True)
class Scores:
    questions: int
    recall_at_10: float
    mrr_at_10: float
    hit_at_1: float


def score_question(
    question: Question, ranked_docs: Sequence[str], canonical: dict[str, str], k: int = K
) -> tuple[float, float, float]:
    """(recall, reciprocal rank, hit@1) for one question.

    ``ranked_docs`` is the document of each retrieved chunk, best first. Exact duplicates count as
    the same document. Recall is the share of the required documents found in the top ``k`` chunks;
    for a question with one required document, finding an alternative document counts as finding it.
    """

    def same(path: str) -> str:
        return canonical.get(path, path)

    required = {same(p) for p in question.source_paths}
    acceptable = required | {same(p) for p in question.alternative_paths}
    top = [same(doc) for doc in ranked_docs[:k]]
    found = required & set(top)
    recall = len(found) / len(required)
    if len(required) == 1 and acceptable & set(top):
        recall = 1.0
    rank = next((i for i, doc in enumerate(top, start=1) if doc in acceptable), None)
    return recall, (1 / rank if rank else 0.0), (1.0 if rank == 1 else 0.0)


def score_all(
    questions: Sequence[Question],
    ranked: Mapping[str, Sequence[str]],
    canonical: dict[str, str],
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


def rank_by_similarity(
    query_vector: Sequence[float],
    chunk_vectors: Sequence[Sequence[float]],
    chunk_docs: Sequence[str],
    k: int = K,
    similarity: Callable[[Sequence[float], Sequence[float]], float] = cosine,
) -> list[str]:
    """The documents of the ``k`` chunks most similar to the query, best first."""
    scored = sorted(
        range(len(chunk_vectors)),
        key=lambda i: similarity(query_vector, chunk_vectors[i]),
        reverse=True,
    )
    return [chunk_docs[i] for i in scored[:k]]
