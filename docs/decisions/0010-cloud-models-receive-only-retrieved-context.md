# ADR 0010: Cloud models receive only retrieved context

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, sections 9 and 10

## Context

Answering questions needs a strong language model, which means calling Anthropic or OpenAI. The documents are private (ADR 0001), and document text is untrusted: it may contain instructions aimed at the model.

## Decision

The only data that leaves the PC is the question, recent turns of that conversation, and the specific passages shown in the answer's sources panel, sent to the provider the user picked. Later, and only if enabled, the opening section of a document may be sent once for metadata suggestions. The rules:

- **Egress gate**: all outbound AI calls go through one function that receives the chunk ids about to be sent, checks each document's and label's `local_only` flag, refuses or drops restricted chunks, and logs which chunk ids were sent. Local Only collections later are a UI toggle on top of it. The UI states what will be sent before it is sent.
- **No capabilities for the model in the MVP**: it cannot fetch URLs, read files or trigger actions. Any future tool use needs explicit confirmation of each action.
- **Structural separation**: instructions live only in the system prompt. Source text goes in delimited, escaped blocks and the system prompt says it is data, never instructions.
- **Safe rendering**: answers render as sanitized Markdown, with no raw HTML and no automatic loading of remote images.
- **Output validation**: citations are validated by the server (ADR 0011).
- **Keys** live in Windows Credential Manager, never in files, logs, the database or the browser.
- **Logs** record ids, counts, timings and errors, never document text or prompts.

## Alternatives considered

- **Send whole documents**: more context, but far more private data leaves the machine and costs more.
- **Cloud embeddings, OCR or rerankers**: would send every chunk or page to a provider (ADR 0001, ADR 0007).
- **No cloud at all**: not enough answer quality today; the provider interface keeps this open (ADR 0009).

## Consequences

- Answer quality is bounded by retrieval quality, which is why retrieval is measured (ADR 0007).
- A document saying "send my files somewhere" can at worst produce a strange answer, not an action.
- The gate has to be the only path to a provider. Reviews should treat any other import of a provider SDK as a defect.
