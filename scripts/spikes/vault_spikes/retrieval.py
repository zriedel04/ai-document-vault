"""Score retrieved chunks: Recall@10 and MRR by passage, Hit@1 by document."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from vault_spikes.corpus import Chunk, Question, Source
from vault_spikes.errors import OwnValueError
from vault_spikes.metrics import cosine, mean

K = 10


@dataclass(frozen=True)
class Scores:
    questions: int
    recall_at_10: float
    mrr_at_10: float
    hit_at_1: float | None
    hit_at_1_questions: int


def _normalise(text: str) -> str:
    return " ".join(text.split())


def covers(chunk: Chunk, source: Source, canonical: Mapping[str, str]) -> bool:
    """Does this chunk hold the passage the source names?

    The chunk must come from the same document (exact duplicates count as one document) and its
    text must contain the whole quote, with whitespace and line breaks ignored. Half a quote is not
    the passage: a chunk with only the start of a sentence says nothing of how it ends. Quotes are
    kept whole in some chunk by the chunker's overlap (``corpus.overlap_for``), and a passage no
    chunk holds is counted and reported by ``sources_missing_from``, not silently scored. Which
    page the quote is on does not matter: the quote itself identifies the passage.
    """
    if canonical.get(chunk.doc_path, chunk.doc_path) != canonical.get(source.path, source.path):
        return False
    words = _normalise(source.quote)
    return bool(words) and words in _normalise(chunk.text)


def finds_a_document(question: Question, canonical: Mapping[str, str]) -> bool:
    """Is this a "find the document" question, as the eval file says, with its evidence in one
    document?

    Hit@1 is defined at document level for these only (``docs/architecture.md``, retrieval
    evaluation). An ordinary factual question whose answer happens to sit in one document is not
    one, so the eval file marks them (``find_document: true``) and the mark is never guessed. The
    evidence check is kept as well: questions that need several documents have no single document
    for the top chunk to be right about.
    """
    return (
        question.find_document
        and len({canonical.get(s.path, s.path) for s in question.sources}) == 1
    )


def document_hit(
    question: Question, ranked: Sequence[Chunk], canonical: Mapping[str, str]
) -> float:
    """1.0 if the top chunk comes from a document that holds the question's evidence, else 0.0.

    Which passage of that document the chunk holds does not matter here; that is Recall@10 and
    MRR. A document that holds only an alternative passage counts, as it does for the other scores.
    """
    if not ranked:
        return 0.0
    right = {canonical.get(s.path, s.path) for s in (*question.sources, *question.alternatives)}
    return 1.0 if canonical.get(ranked[0].doc_path, ranked[0].doc_path) in right else 0.0


def score_question(
    question: Question,
    ranked: Sequence[Chunk],
    canonical: Mapping[str, str],
    k: int = K,
) -> tuple[float, float]:
    """(recall, reciprocal rank) for one question, judged by passage.

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
    return recall, (1 / rank if rank else 0.0)


def score_all(
    questions: Sequence[Question],
    ranked: Mapping[str, Sequence[Chunk]],
    canonical: Mapping[str, str],
    k: int = K,
) -> Scores:
    """Average the per-question scores over the questions that have a ranking.

    Recall and MRR cover every such question. Hit@1 covers only the find-the-document questions
    and is None when there are none, so it is left out of the report instead of reading as 0.
    """
    have = [q for q in questions if q.id in ranked]
    results = [score_question(q, ranked[q.id], canonical, k) for q in have]
    if not results:
        msg = "no question could be scored"
        raise OwnValueError(msg)
    hits = [
        document_hit(q, ranked[q.id], canonical) for q in have if finds_a_document(q, canonical)
    ]
    return Scores(
        len(results),
        mean([r[0] for r in results]),
        mean([r[1] for r in results]),
        mean(hits) if hits else None,
        len(hits),
    )


def can_be_scored(question: Question, readable: frozenset[str]) -> bool:
    """Can this question be ranked at all, given the documents the run could read?

    Every required source must be readable. The exception follows ``score_question``: a question
    with one required passage is also answered in full by any of its alternatives, so it can be
    scored when the primary document is unreadable (a scan, say) but an alternative one is not.
    A question that needs several passages is never saved by an alternative.
    """
    if not (question.answerable and question.sources):
        return False
    if set(question.source_paths) <= readable:
        return True
    return len(question.sources) == 1 and any(a.path in readable for a in question.alternatives)


def sources_missing_from(
    questions: Sequence[Question],
    chunks: Sequence[Chunk],
    canonical: Mapping[str, str],
    readable: frozenset[str],
) -> int:
    """How many required passages of a readable document no chunk of the corpus holds at all.

    Such a passage can never be retrieved, whatever the model does (a quote that is mistyped, text
    the reader cannot extract, a quote longer than the chunk overlap that a boundary cut), so the
    count is reported next to the scores instead of silently lowering them. A passage in a
    document the run could not read is not counted: that is the reader's limit, already visible in
    the skipped documents, and the question is scored by an alternative or left out.
    """
    return sum(
        1
        for q in questions
        for source in q.sources
        if source.path in readable
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
