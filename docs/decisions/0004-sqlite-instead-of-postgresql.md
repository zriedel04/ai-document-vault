# ADR 0004: SQLite instead of PostgreSQL

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, sections 3, 5 and 6, and section 22 (decision 3)

## Context

The app has one user on one machine and a realistic scale of tens of thousands of documents. It needs relational storage, keyword search with ranking, and vector search. A database server would be one more Windows service to install, upgrade and back up.

## Decision

Use SQLite in WAL mode, accessed through SQLAlchemy 2 Core with Alembic migrations, behind a repository layer. Full-text search uses FTS5 (BM25, porter and unicode61 tokenizer). Vector search uses sqlite-vec with exact (brute-force) KNN in a separate `index.db`, so vectors are rebuildable and left out of backups. `vault.db` holds everything irreplaceable (ADR 0006).

## Alternatives considered

- **PostgreSQL with pgvector**: more capable at scale, but a service to install and maintain for capability this app does not need yet.
- **LanceDB**: embedded with ANN indexes, but a second storage engine.
- **Chroma or Qdrant**: extra services.
- **Tantivy, Meilisearch or Elasticsearch for text search**: separate services or bindings, and unnecessary at this scale.
- **SQLite's own `vec1` extension**: not yet 1.0.

## Consequences

- One file to back up, no server, fast reads.
- One writer at a time. Fine for one user: the worker batches writes.
- Exact vector search is accurate and fast enough up to roughly 300k chunks. Beyond that it needs an ANN index or quantization (plan section 16).
- sqlite-vec is pre-1.0 and its ANN indexes are alpha. Vectors are rebuildable, so the risk is low. Loading the extension on Windows is confirmed in the Phase 0 dependency spike.
- The repository layer keeps a later move to PostgreSQL contained.
