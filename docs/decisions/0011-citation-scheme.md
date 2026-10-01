# ADR 0011: Citation scheme

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 11

## Context

Every answer must point back to the exact document, page and passage it came from, and must say so when the vault does not contain enough evidence. A model cannot be trusted to produce correct ids or quotes on its own.

## Decision

Chunks in a prompt get session-local labels `[S1]` to `[Sn]`. The model returns structured output: segments of the answer, each with its supporting source labels and a short quote, plus an explicit `insufficient_evidence` flag. The server, not the model, resolves labels to database ids (chunk, version, document, pages, character offsets) and validates them: a label that was not provided is removed and the segment marked unsupported, a quote that cannot be found in the chunk text (matched tolerantly) is shown as "could not verify", and uncited segments are shown muted. Every passage the model saw is stored in `message_sources` with its rank and scores and a text snapshot, so citations survive document changes and deletion. When retrieval is weak or the flag is set, the UI says the vault does not hold enough to answer and shows the closest passages.

This provider-neutral scheme is the baseline for both providers. The Anthropic adapter may switch to native citations later if the evaluation shows better accuracy.

## Alternatives considered

- **Free-text citations written by the model**: cannot be verified and invites invented references.
- **Provider-native citation features only**: behave differently per provider and cannot be validated the same way.
- **Post-hoc attribution** (match answer sentences to chunks afterwards): weaker evidence of what actually supported each claim.

## Consequences

- Both providers must support structured output. Both do.
- Citation precision and abstention correctness are measured on the evaluation set.
- Snapshots mean the database keeps copies of cited passages, so they are covered by the privacy and backup rules for `vault.db`.
