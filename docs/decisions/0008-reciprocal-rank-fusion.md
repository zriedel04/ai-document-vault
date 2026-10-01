# ADR 0008: Reciprocal Rank Fusion

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 8

## Context

Hybrid retrieval (ADR 0007) produces two ranked lists whose scores are not comparable: BM25 scores and cosine similarities have different ranges and distributions.

## Decision

Merge the lists with Reciprocal Rank Fusion: the score of a chunk is the sum over lists of `1 / (k + rank)`. Small configurable boosts (title or label match, recency) can be added. `k`, the candidate counts and the boosts are configuration values tuned against the evaluation set.

## Alternatives considered

- **Weighted blend of normalised scores**: needs score calibration between two very different scales and tends to break when either retriever changes.
- **Learned fusion or a trained ranker**: needs training data this project does not have.
- **Reranker only**: see ADR 0007.

## Consequences

- No calibration needed, and it is simple enough to unit-test and explain.
- It uses ranks only, so it ignores how confident either retriever was. If the evaluation set shows this hurts, a new ADR can change the method.
