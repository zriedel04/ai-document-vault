# ADR 0013: Embedding model

- Status: Proposed
- Date: 2026-10-01
- Hardware-dependent: yes
- Plan reference: docs/architecture.md, section 3 (embedding model) and section 22 (decision 7)

## Context

Semantic search needs an embedding model. It must keep document text private (ADR 0001), cost nothing per document, and run on the owner's PC. Whether a given model is fast enough, and good enough on the owner's kinds of documents, depends on the Windows PC's CPU, RAM and GPU, which are not yet known.

## Decision

Proposed: run Qwen3-Embedding-0.6B locally (Apache-2.0, 1024 dimensions, 32k context) through sentence-transformers or ONNX Runtime. The embedding model id is stored with every vector, so switching models means a background re-embed into a new `chunk_vectors_<model>` table, not a redesign. EmbeddingGemma-300M (smaller and faster, 2k context, Gemma Terms of Use) is the fallback if the PC is slow.

This stays Proposed until the embedding spike has run on the Windows PC.

## Evidence needed

- Windows PC specifications from the owner.
- The embedding spike (issue #8) on that PC: embedding throughput in chunks per second on CPU and, if present, GPU, memory use, model load time, and a projection of the time to embed a vault of the owner's size.
- Retrieval quality of each candidate on the public synthetic evaluation set (Recall@10 and MRR), and on a sample of the owner's documents with results reported as metrics only.
- Results go in `docs/spikes/` as metrics, never document text. If the proposal does not hold up, the results and a recommended alternative go to the owner before anything changes.

## Alternatives considered

- **EmbeddingGemma-300M**: lighter, but a shorter context and a custom licence.
- **BGE-M3** (MIT): older.
- **OpenAI `text-embedding-3-small` through the API**: cheap and easy, but every chunk leaves the machine, against ADR 0001.

## Consequences

- Until accepted, no code may assume a specific model beyond the stored model id and a configurable setting.
- CPU-only backfill of a large vault may take hours (one time, in the background).
