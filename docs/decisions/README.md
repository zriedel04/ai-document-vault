# Architecture decision records

Each file here records one decision: the context, what was decided, the alternatives considered and the consequences. The approved plan is [`docs/architecture.md`](../architecture.md). Where an ADR and the plan disagree, the ADR is newer and wins.

## Statuses

| Status | Meaning |
| --- | --- |
| Proposed | Recommended but not final. Hardware-dependent choices stay here until their spike has run on the owner's Windows PC |
| Accepted | Decided. Code may rely on it |
| Rejected | Considered and not adopted. Kept so the reasoning is not lost |
| Superseded by ADR NNNN | Replaced by a newer decision |

## Rules

- A change to an Accepted decision needs a new ADR that supersedes it, and the owner's approval before the change is made.
- Nothing marked `Hardware-dependent: yes` may be Accepted until a spike result is linked from the ADR (`- Spike results: docs/spikes/<file>.md`). See [`docs/spikes/`](../spikes/README.md).
- A Proposed ADR has an "Evidence needed" section saying what would settle it.
- Copy [`0000-template.md`](0000-template.md) for a new ADR and add it to the index below. `python scripts/check_adrs.py` checks the structure, the statuses and this index, in pre-commit and in CI.

## Index

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-local-first-design.md) | Local-first design | Accepted |
| [0002](0002-local-web-app.md) | Local web app instead of a native desktop app | Accepted |
| [0003](0003-native-windows.md) | Native Windows without Docker or WSL2 | Accepted |
| [0004](0004-sqlite-instead-of-postgresql.md) | SQLite instead of PostgreSQL | Accepted |
| [0005](0005-in-process-job-queue.md) | In-process job queue instead of Redis and Celery | Accepted |
| [0006](0006-content-addressed-storage.md) | Managed, content-addressed document storage | Accepted |
| [0007](0007-hybrid-retrieval.md) | Hybrid retrieval | Accepted |
| [0008](0008-reciprocal-rank-fusion.md) | Reciprocal Rank Fusion | Accepted |
| [0009](0009-no-ai-frameworks.md) | No LangChain or LlamaIndex | Accepted |
| [0010](0010-cloud-models-receive-only-retrieved-context.md) | Cloud models receive only retrieved context | Accepted |
| [0011](0011-citation-scheme.md) | Citation scheme | Accepted |
| [0012](0012-licence-policy-and-pdf-libraries.md) | Licence policy and PDF libraries | Accepted |
| [0013](0013-embedding-model.md) | Embedding model | Proposed |
| [0014](0014-ocr-engine.md) | OCR engine | Proposed |
| [0015](0015-pdf-extraction-quality.md) | PDF extraction quality | Proposed |
