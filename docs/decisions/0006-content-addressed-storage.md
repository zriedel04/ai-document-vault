# ADR 0006: Managed, content-addressed document storage

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, sections 5 and 6

## Context

A document can belong to several collections at once, the same file can be imported from several places, and the owner's originals must never be altered. Backups should be small, and changing the embedding model must not mean redesigning storage.

## Decision

Imports copy the original byte-for-byte into the vault as `blobs/sha256/aa/bb/<hash>.<ext>`, marked read-only. The human filename, source path and folder hierarchy live in the database (`import_sources`), never in the blob name. Data is split by value:

- Irreplaceable, backed up: `blobs/`, `vault.db` (metadata, labels, user edits, chunks and FTS5, jobs, conversations, usage, AI caches) and `config.toml`.
- Rebuildable: `index.db` (embeddings) and `derived/` (page text JSON, OCR JSON, thumbnails).

An Export action writes a normal folder tree with human filenames plus a JSON/CSV manifest, so there is no lock-in. The vault folder is never inside the code repository: the app refuses to start if the configured vault folder is inside a Git working tree.

## Alternatives considered

- **Readable folder tree**: breaks as soon as a document belongs to several places, and names can collide or traverse paths.
- **Index files where they already are**: the vault would break when the owner moves or renames folders.
- **Store files inside the database**: makes the database huge and backups slow.

## Consequences

- Deduplication, integrity checks (re-hash) and safe file names come for free.
- Browsing the vault in Explorer is not meaningful. Export covers that need.
- Every derived artifact records the hash and tool version it came from, so only the dependent stages are re-run when something changes.
