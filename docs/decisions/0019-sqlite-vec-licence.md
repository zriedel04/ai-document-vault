# ADR 0019: sqlite-vec licence: MIT OR Apache-2.0

- Status: Accepted
- Date: 2026-11-17
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 21 (Licences: "sqlite-vec | MIT or Apache-2.0 | OK") and section 3 (stack table, vector search)

## Context

ADR 0004 chooses sqlite-vec for the vectors in `index.db`, and the approved plan's licence table lists it as "MIT or Apache-2.0". Neither records how the CI licence gate and the third-party notices read that licence, and the package makes both hard:

- The licence gate (`scripts/check_python_licenses.py`) reads a package's SPDX expression, then its licence classifiers, then a short `License` field. sqlite-vec 0.1.9 has no expression and no classifiers, and writes its `License` field as free text: `MIT License, Apache License, Version 2.0`. The check cannot parse that, so it fails the package unless a reviewed translation names it.
- The notices (`scripts/third_party_notices.py`) carry the licence files a package installs. The sqlite-vec 0.1.9 wheels ship none. Both the manylinux x86_64 and win_amd64 wheels were listed, and each holds only `sqlite_vec/__init__.py`, the compiled extension (`vec0.so` or `vec0.dll`) and the `.dist-info` metadata. A runtime package with no licence file fails the notices check unless a reviewed text fills the gap.

The upstream repository (github.com/asg017/sqlite-vec) says the project is dual-licensed and holds `LICENSE-MIT` and `LICENSE-APACHE` at tag v0.1.9. The extension is built from sqlite-vec's own C source and links no other library into the wheel.

## Decision

- sqlite-vec 0.1.9 is a runtime dependency of the backend, used under **MIT OR Apache-2.0**, both on the allowed list (ADR 0012).
- The licence gate reads the exact field `MIT License, Apache License, Version 2.0` as the expression `MIT OR Apache-2.0` through `TRANSLATED`, for this one package and this exact value. A different field, or an expression or classifiers added later, is judged by those instead. The translation holds under `--strict`, because the expression must still pass the allowed list.
- The notices carry the upstream `LICENSE-MIT` and `LICENSE-APACHE` from tag v0.1.9, stored together in `scripts/notices/sqlite-vec.LICENSE` and listed in the script's `OVERRIDES` with where they came from. If a later wheel ships its own licence file, the notices check fails until the override is removed.

## Alternatives considered

- **Treat the package as a reviewed exception (`REVIEWED`)**: exceptions are for development tools only and are refused by `--strict` on the runtime environment, which is where sqlite-vec belongs.
- **Name the licence in ADR 0004**: an Accepted ADR is changed only by one that supersedes it, and the storage decision itself is unchanged.
- **Accept the plan's licence table as the source in the check's test**: this would widen what the test accepts for every future translation. Keeping each translation tied to an ADR that names the package and its licences is the stricter rule.
- **A different vector store**: out of scope. ADR 0004 already decides this.

## Consequences

- The licence gate and the notices check pass on Linux and Windows with sqlite-vec installed, and fail again if its metadata or its shipped files change.
- An update of sqlite-vec that keeps the same field and ships no licence file needs only a check that the upstream texts are unchanged. If they changed, `scripts/notices/sqlite-vec.LICENSE` is refreshed from the new tag.
- The licence audit (`docs/licence-audit.md`) lists sqlite-vec among the runtime packages and points here.
