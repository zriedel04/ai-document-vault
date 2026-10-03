"""Spike 2: embedding speed and retrieval quality of the candidate models on this PC.

Decides ADR 0013. For each candidate the script measures load time, throughput in chunks per
second, peak memory, a projection for a vault of a given size, and Recall@10, MRR and Hit@1 on
the public eval questions. The models are injected (:class:`Embedder`), so the unit tests run the
whole spike with a fake and CI never downloads a model.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from vault_spikes.corpus import DEFAULT_OVERLAP, Chunk, Corpus, Document, Question, canonical_paths
from vault_spikes.metrics import gpu_peak_memory_mib, mean, peak_memory_mib, per_second
from vault_spikes.report import Environment, Report, collect_environment
from vault_spikes.retrieval import K, rank_by_similarity, score_all, sources_missing_from

TITLE = "Embedding models on this PC"
QUESTION = (
    "How fast does each candidate embedding model run on this PC (CPU and GPU if present), how "
    "much memory does it need, how long would a vault of the stated size take, and how well does "
    "it retrieve on the eval questions? Feeds ADR 0013."
)
METHOD = (
    "`python -m vault_spikes embedding --candidate NAME --device DEVICE`, one candidate per "
    "process so the peak memory is that candidate's. The model is downloaded beforehand with "
    "`fetch-model` and loaded only from the local cache, so the load time never includes a "
    "download. Memory is the process's peak working set (host RAM); a CUDA run also reports the "
    "peak video memory torch allocated and reserved, since the working set does not include it. "
    "The corpus is cut "
    "into chunks of at most 800 characters that overlap by at least the longest evidence quote, "
    "so no quote is split between two chunks (the overlap is reported); throughput is chunks "
    "embedded per second in batches. "
    "Each answerable public question is embedded and its top 10 chunks are ranked by cosine "
    "similarity. Retrieval is scored by passage: a chunk counts only if it comes from the right "
    "document and holds the quote the eval file names as the evidence. Recall@10 is the share of "
    "required passages found, MRR@10 the reciprocal rank of the first chunk holding an acceptable "
    "passage. Hit@1 is scored by document and only for the questions the eval file marks as "
    "find-the-document questions (`find_document: true`, evidence in one document): whether the "
    "top chunk comes from a document that holds it. Required passages that "
    "no chunk holds at all are counted separately. Questions whose "
    "documents the script cannot read (scans, pictures) are left out and counted. The "
    "`public_dataset` metric is true only when the corpus and the questions are the repository's "
    "public synthetic ones (`demo/corpus/`, `eval/public/`); otherwise they are the owner's own "
    "files, kept outside the repository, and only numbers about them are reported."
)
PACKAGES = ("sentence-transformers", "torch", "transformers", "numpy")
# What a report of this spike may contain (checked when a report is read back for a merge).
CANDIDATES = re.compile(r"(?:qwen3-embedding-0\.6b|embeddinggemma-300m)-(?:cpu|cuda)")
METRICS = frozenset(
    {
        "dimensions",
        "load_seconds",
        "chunks",
        "chunk_overlap_chars",
        "mean_chunk_chars",
        "chunks_per_second",
        "hours_for_vault_of_chunks",
        "vault_chunks_assumed",
        "questions_scored",
        "questions_left_out",
        "sources_not_in_any_chunk",
        "public_dataset",
        "recall_at_10",
        "mrr_at_10",
        "hit_at_1_questions",
        "hit_at_1",
        "peak_memory_mib",
        "gpu_peak_allocated_mib",
        "gpu_peak_reserved_mib",
    }
)


class Embedder(Protocol):
    """A model that turns text into vectors. ``name`` is a label fixed in code."""

    name: str

    def load(self) -> None: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingInputs:
    corpus: Corpus
    documents: list[Document]
    questions: list[Question]
    chunk_overlap_chars: int = DEFAULT_OVERLAP


def run_embedding_spike(
    embedder: Embedder,
    inputs: EmbeddingInputs,
    *,
    batch_size: int = 16,
    vault_chunks: int = 100_000,
    public_dataset: bool = True,
    environment: Environment | None = None,
    clock: Callable[[], float] = time.perf_counter,
    gpu_memory: Callable[[], tuple[float, float] | None] = gpu_peak_memory_mib,
) -> Report:
    """Measure one candidate and return a report with one row."""
    corpus = inputs.corpus
    if not corpus.chunks:
        msg = "the corpus has no readable chunks"
        raise ValueError(msg)
    report = Report(
        "embedding",
        TITLE,
        QUESTION,
        METHOD,
        environment or collect_environment(PACKAGES),
    )

    begin = clock()
    embedder.load()
    load_seconds = clock() - begin

    texts = [chunk.text for chunk in corpus.chunks]
    embedder.embed_documents(texts[:batch_size])  # warm-up, not timed: the first call allocates
    begin = clock()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(embedder.embed_documents(texts[start : start + batch_size]))
    embed_seconds = clock() - begin
    if len(vectors) != len(texts):
        msg = "the model returned the wrong number of vectors"
        raise RuntimeError(msg)
    chunks_per_second = per_second(len(texts), embed_seconds)

    readable = corpus.readable
    scored = [
        q
        for q in inputs.questions
        if q.answerable and q.source_paths and set(q.source_paths) <= readable
    ]
    canonical = canonical_paths(inputs.documents)
    ranked: dict[str, list[Chunk]] = {}
    if scored:
        query_vectors = embedder.embed_queries([q.text for q in scored])
        for question, vector in zip(scored, query_vectors, strict=True):
            ranked[question.id] = rank_by_similarity(vector, vectors, corpus.chunks, K)
    scores = score_all(scored, ranked, canonical) if scored else None

    metrics: dict[str, int | float] = {
        "dimensions": len(vectors[0]),
        "load_seconds": load_seconds,
        "chunks": len(texts),
        "chunk_overlap_chars": inputs.chunk_overlap_chars,
        "mean_chunk_chars": mean([len(t) for t in texts]),
        "chunks_per_second": chunks_per_second,
        "hours_for_vault_of_chunks": vault_chunks / chunks_per_second / 3600,
        "vault_chunks_assumed": vault_chunks,
        "questions_scored": scores.questions if scores else 0,
        "questions_left_out": len([q for q in inputs.questions if q.answerable])
        - (scores.questions if scores else 0),
        "sources_not_in_any_chunk": sources_missing_from(scored, corpus.chunks, canonical),
        "public_dataset": public_dataset,
    }
    if scores:
        metrics |= {
            "recall_at_10": scores.recall_at_10,
            "mrr_at_10": scores.mrr_at_10,
            "hit_at_1_questions": scores.hit_at_1_questions,
        }
        if scores.hit_at_1 is not None:
            metrics["hit_at_1"] = scores.hit_at_1
    if (peak := peak_memory_mib()) is not None:
        metrics["peak_memory_mib"] = peak
    # The host number above is the process's RAM. A candidate run on the GPU also needs video
    # memory, which is what limits it on a 12 GB card, so it is reported separately.
    if embedder.name.endswith("-cuda") and (gpu := gpu_memory()) is not None:
        metrics["gpu_peak_allocated_mib"], metrics["gpu_peak_reserved_mib"] = gpu
    report.add(embedder.name, **metrics)
    return report
