# Spike 01: sqlite-vec on this PC

Run by the owner on his Windows PC on 2026-10-17 with `scripts/spikes` (`spike_vec.py`, "Spike 1"). Metrics only: the vectors are random numbers made by the script, so no document, extracted text or embedding of a document is involved. The Results table is the one the script wrote; the derived figures and the conclusion below it were added afterwards and are marked as such.

## Question

Does the sqlite-vec extension load into this Python's sqlite3 on the owner's Windows PC, and what are the insert rate, database size and exact (brute-force) search latency at 10,000 and 100,000 vectors of 1,024 dimensions?

It feeds [ADR 0004](../decisions/0004-sqlite-instead-of-postgresql.md) (SQLite, with sqlite-vec exact KNN in a separate `index.db`), whose consequences say that loading the extension on Windows is confirmed in the Phase 0 spike and that exact search is fast enough up to roughly 300k chunks. ADR 0004 is Accepted and not hardware-dependent; this spike checks its assumptions and does not change it.

## Environment

As the report script recorded it:

- System: Windows 11
- Python: 3.12.15
- CPU: AMD64 Family 25 Model 97 Stepping 2, AuthenticAMD (16 logical cores). The owner identifies it as an AMD Ryzen 7 7800X3D; the script records only the processor identifier, so the model name is the owner's statement.
- RAM: 63.2 GiB
- GPU: none reported (not used by this spike)
- sqlite-vec: 0.1.9 (the version ADR 0004 and `docs/architecture.md` section 3 name)

## Method

- Command, from `scripts\spikes`: `uv run python -m vault_spikes vec` (the report's Method line gives it as `python -m vault_spikes vec`). The rows match the command's defaults: sizes 10,000 and 100,000, 1,024 dimensions, 30 timed queries per size, 10 neighbours per query, a 500-vector correctness check with 5 probe queries.
- Data set: synthetic only. Random float32 vectors from a seeded generator (values in [0, 1), not normalised), inserted into a `vec0` table (`embedding float[1024]`) in a temporary database file, 1,000 rows per transaction. Only the inserts are timed, not the generation of the numbers.
- Latency: wall-clock time of each `WHERE embedding MATCH ? AND k = 10 ORDER BY distance` query (sqlite-vec's default L2 distance), 30 queries per size, reported as the median (p50) and 95th percentile (p95). The queries run straight after the inserts in the same process, so the file was recently written and the reads are warm; a first query after a restart is not measured.
- Database size: the file size after a WAL checkpoint.
- Peak memory: the process's peak working set, a high-water mark over the whole run (sizes are run from smallest to largest so each row's peak is its own). It does not include the operating system's file cache.
- Correctness: 5 random queries over a separate set of 500 vectors, each answer compared place by place with a plain-Python exact search (ties within float32 rounding may swap). `same_answer_as_python` is the share of the 5 that matched.
- What it does not measure: filter columns or partitions (the planned `chunk_vectors_<model>` table has `document_id` and `local_only`, plan section 6), `k` above 10, other dimensions than 1,024, cosine distance, concurrent writes during search, or a cold start. See `scripts/spikes/README.md` for the full description of the script.

## Results

Copied unchanged from the Markdown draft the script wrote (`vec.md`, not committed):

| Candidate | can_load_extensions | database_mib | dim | insert_vectors_per_second | peak_memory_mib | queries_timed | query_ms_p50 | query_ms_p95 | same_answer_as_python | vec_loads | vectors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| extension-loading | True |  |  |  |  |  |  |  |  | True |  |
| exact-search-check |  |  |  |  |  |  |  |  | 1.0 |  | 500 |
| vectors-10000 |  | 40.2891 | 1024 | 2481.6422 | 53.3164 | 30 | 28.1213 | 32.4361 |  |  | 10000 |
| vectors-100000 |  | 394.5938 | 1024 | 1997.1339 | 53.9219 | 30 | 300.4529 | 317.6911 |  |  | 100000 |

In words: sqlite-vec loads into this Python; its exact search gave the same ranked ten neighbours as the plain-Python search on 5 of 5 queries; 10,000 vectors take 40.3 MiB, insert at about 2,480 per second and answer in 28.1 ms (p50) / 32.4 ms (p95); 100,000 vectors take 394.6 MiB, insert at about 2,000 per second and answer in 300.5 ms (p50) / 317.7 ms (p95); the process peaked at about 54 MiB at both sizes.

### Derived figures (added, not written by the script)

Computed from the table above:

| Figure | Value | How |
| --- | --- | --- |
| Raw vector bytes | 39.1 MiB at 10k, 390.6 MiB at 100k | `vectors * 1024 * 4`; the files are 3.1% and 1.0% larger than their vectors |
| Latency growth, 10k to 100k | 10.7x at p50, 9.8x at p95 | ratio of the two rows, for 10x the vectors: close to linear, as expected of a brute-force scan |
| Cost per stored value | about 2.9 ns | 300.45 ms / (100,000 x 1,024) |
| Time to insert 100k vectors | about 50 s | 100,000 / 1,997 per second (insert rate fell about 20% from 10k to 100k) |
| Memory | about 54 MiB peak at both sizes | the process did not hold the 394.6 MiB of vectors in its own memory |

### Comparison with the plan's targets and estimates

The plan's estimates (section 16) assume 20 chunks per document and 1,024-dimension vectors. Latencies below for sizes other than 10k and 100k are **linear extrapolations** from the 100k row (3.0 ms per 1,000 vectors at 1,024 dimensions), not measurements.

| Source | Target or estimate | Measured or extrapolated | Holds? |
| --- | --- | --- | --- |
| Plan section 13, Phase 4 "Done when" | A 10k-chunk search returns in under a second | Vector part: 28.1 ms p50, 32.4 ms p95 (measured) | Yes, for the vector part. The full hybrid search also embeds the query, runs FTS5 and fuses; those are not measured here |
| Plan section 16, 1,000 documents (~20k chunks) | Vector search well under 100 ms | ~60 ms (extrapolated) | Yes |
| Plan section 16, 10,000 documents (~200k chunks) | Exact vector search roughly 100-300 ms; vectors ~800 MB | ~600 ms (extrapolated); ~789 MiB (extrapolated from 394.6 MiB per 100k) | Size yes; latency about twice the estimate |
| Plan section 16, 50,000 documents (~1M chunks) | Exact vector search roughly 1 s or more | ~3 s (extrapolated) | Yes ("or more"); quantization or ANN needed, as the plan says |
| ADR 0004 consequences, plan section 3 | Exact search fast enough up to roughly 300k chunks | ~900 ms per query at 300k (extrapolated) | Only if about 1 s per vector query is acceptable; no latency target exists for that size |
| ADR 0004 consequences | Loading the extension on Windows confirmed in Phase 0 | `can_load_extensions` and `vec_loads` True | Yes |

## Conclusion

### Recommended conclusion (awaiting the owner's confirmation)

**Status: recommended by Claude Code, not yet confirmed by the owner.** Nothing in the code or the ADRs relies on it until the owner confirms it.

1. **Keep ADR 0004 as decided.** sqlite-vec 0.1.9 loads into Python 3.12.15 on the owner's Windows 11 PC, and its exact search returned the correct ranking on every probe. Exact (brute-force) KNN is comfortably fast at the scale the MVP targets: 28 ms per query at 10,000 vectors, far inside the Phase 4 target of under a second for a 10k-chunk search, and about 60 ms (extrapolated) for the plan's 1,000-document vault.
2. **The point where exact search gets slow comes sooner than the plan estimated.** Latency grows about linearly with the number of vectors: 300 ms per query at 100,000 vectors of 1,024 dimensions. A 10,000-document vault (~200k chunks) would take about 600 ms per vector query, not the plan's 100-300 ms, and the plan's 300k-chunk limit is about 900 ms. Practically, exact search stays under about 300 ms up to roughly 100k chunks (about 5,000 documents at the plan's 20 chunks per document) with 1,024 dimensions.
3. **Latency should scale with the dimension count too** (an assumption: only 1,024 dimensions were measured). A 768-dimension model would take about 225 ms at 100k vectors and a 512-dimension truncation (which the plan says Qwen3 supports) about 150 ms. The embedding model is still open (ADR 0013, Proposed), so its dimension count is one of the inputs to this limit.

**What would change the decision or its limits:**

- The vault growing past roughly 100k-150k chunks, or a measured end-to-end search target below what the extrapolation gives: then quantization (int8 or truncated dimensions) or an approximate index, as plan section 16 already lists, would be needed earlier than "past 300k chunks". ADR 0004 already allows for this; it would be recorded in the vector store module and, if the storage choice changes, in a new ADR.
- sqlite-vec's ANN indexes leaving alpha, which would make an approximate index available without a second storage engine.
- The Phase 4 measurements with real chunks, the chosen embedding model's dimensions, filter columns and the real `k`, which replace these synthetic figures.

**Suggested follow-ups once confirmed** (not done in this change): update the 10,000- and 50,000-document latency estimates in `docs/architecture.md` section 16 with these measurements, and, if the owner wants the dimension assumption checked, run `uv run python -m vault_spikes vec --dim 768` (and `--dim 512`) on the same PC.
