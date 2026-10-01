# ADR 0001: Local-first design

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, sections 1, 5 and 10

## Context

The vault holds personal documents: coursework, receipts, screenshots, correspondence. It is meant to be permanent external memory for AI, so the documents are both sensitive and long-lived. Any design that uploads documents to a service puts all of them in someone else's hands, permanently.

## Decision

Everything that makes up the vault stays on the owner's PC: original files, extracted text, OCR output, the databases, embeddings, indexes, conversations, usage data, configuration and API keys. Extraction, OCR, chunking, keyword search, embeddings and retrieval run locally with no API calls. A document is searchable before any AI has touched it ("deterministic first, AI second"). The cloud is used only to answer questions and, later and opt-in, to suggest metadata, and then only with the data described in ADR 0010.

## Alternatives considered

- **Cloud-hosted service with accounts and uploads**: easiest remote access, but every document leaves the machine and a service must be operated.
- **Local app with cloud embeddings or OCR** (for example OpenAI `text-embedding-3-small`): faster to set up, but sends every chunk of every document to a provider.
- **Fully offline, including a local language model**: strongest privacy, but local model quality is not yet enough for the answer step. A `LocalProvider` can be added later behind the same interface (ADR 0009).

## Consequences

- Document text stays private and there is no per-document cost for processing.
- OCR and embedding run on the owner's hardware. A CPU-only backfill of a large vault can take hours (one time, in the background).
- The owner is responsible for backups. The storage layout separates irreplaceable data from rebuildable data to keep them small (ADR 0006).
- Remote access later needs a private tunnel and real authentication (plan section 18), never an open port.
