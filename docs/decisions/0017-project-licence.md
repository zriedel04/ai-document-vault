# ADR 0017: Project licence: Apache-2.0

- Status: Accepted
- Date: 2026-10-15
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 21 (Licences) and section 22 (decision 15)

## Context

ADR 0012 left the project's own licence open until publication, choosing between Apache-2.0 and MIT. The repository was made public on 2026-10-15. A public repository with no licence is "all rights reserved": others may read the code but have no right to use, change or share it, which defeats the portfolio goal.

## Decision

The project is licensed under the Apache License, Version 2.0. The full text is in `LICENSE` at the repository root, and the backend, frontend and demo generator declare `Apache-2.0` in their package metadata.

## Alternatives considered

- **MIT**: shorter and equally permissive, but has no explicit patent grant and no patent termination clause.
- **No licence**: keeps every right but makes the public code unusable by anyone else.

## Consequences

- Contributions are accepted under Apache-2.0 (its section 5), so no separate contributor agreement is needed.
- Every dependency licence that ADR 0012 allows is compatible with distributing the project under Apache-2.0.
- Anyone redistributing the project keeps the licence text and states their changes. A `NOTICE` file is optional and not added. The licence audit (`docs/licence-audit.md`) and the generated `THIRD_PARTY_NOTICES.md` cover the dependencies.
