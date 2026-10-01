# ADR 0005: In-process job queue instead of Redis and Celery

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, sections 2, 3 and 7, and section 22 (decision 4)

## Context

Import, OCR, embedding and AI enrichment are slow and must survive restarts, be pausable, retryable and visible in the UI. Imports of thousands of files must not make the UI unresponsive.

## Decision

A `jobs` table in SQLite is the queue. A small worker pool inside the server process pulls jobs with a concurrency limit per stage (for example extraction 2, OCR 1, embedding 1, AI 2), retries with backoff, and supports pause and resume. A document has a small lifecycle state (Processing, Searchable, Ready, Failed, Trashed) while each processing stage is its own job row. Heavy parsing runs in a child process with a timeout and memory cap so a malformed file cannot hang or crash the server.

## Alternatives considered

- **Redis with Celery**: two extra moving parts, and Celery is not officially supported on Windows.
- **Huey with SQLite**: workable, but a custom queue is about 200 lines and fits the status model exactly.
- **Threads or asyncio tasks without persistence**: lose work on restart and give the UI nothing to show.

## Consequences

- No extra services. Jobs survive crashes: unfinished jobs are picked up again on restart.
- The project owns retry and locking logic. It is small and must be fully tested.
- Scaling past one process is not possible without replacing the queue. That is acceptable for a single-user local app.
