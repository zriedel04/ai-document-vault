# ADR 0007: Hybrid retrieval

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 8, and section 22 (decision 10)

## Context

Questions range from exact terms and codes ("CSE 485", a receipt number) to meaning ("the assignment where we interviewed our sponsor"). Keyword search alone misses paraphrases and vector search alone misses exact strings. Answers must be grounded in retrieved passages.

## Decision

Retrieval runs keyword search (FTS5 BM25) and vector search (local query embedding, exact KNN) in parallel inside the chosen scope, merges them (ADR 0008), diversifies (a maximum number of chunks per document, adjacent chunks merged) and packs the best chunks into a token budget. Scope (whole vault, a label or collection, or selected documents) is applied before ranking, as a SQL filter. Query preparation in the MVP is deterministic (keep quoted phrases, course codes and numbers). The Search page offers Keyword, Semantic and Hybrid modes, with Hybrid as the default. Every parameter (candidate counts, fusion constant, boosts, chunks per document, token budget) lives in configuration and is tuned against the evaluation set, never hard-coded.

A reranker is not part of the MVP. A local cross-encoder is added in a later phase only if the evaluation set shows it helps.

## Alternatives considered

- **Keyword only**: no semantic matching.
- **Vector only**: weak on exact terms, codes and names.
- **Reranker from the start**: adds a model, a download and one to three seconds per question on CPU (an estimate to be measured) before there is a baseline to judge it against.
- **API rerankers (Cohere, Voyage)**: would send five to ten times more document text to a cloud service than the answer step does, which conflicts with ADR 0001 and ADR 0010.

## Consequences

- Needs an evaluation set to be meaningful: a public synthetic one in the repository and a private one in the vault folder (Recall@10, MRR, Hit@1 at document level, citation precision, abstention correctness).
- Some question types stay weak in the MVP: date-structured questions, whole-project synthesis and version comparison (plan section 8).
- Every retrieval step can write a trace (ids and scores) for a debug view.
