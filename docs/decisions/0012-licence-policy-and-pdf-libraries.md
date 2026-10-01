# ADR 0012: Licence policy and PDF libraries

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 21 (Licences) and section 22 (decision 12)

## Context

The repository is private for now but is built to go public as a portfolio project. PyMuPDF, the fastest and richest PDF library, is AGPL-3.0, which would bind a public repository and anyone running a modified network version. Surya OCR is GPL-3.0.

## Decision

- Dependencies must use permissive licences: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0, Python-2.0, Unlicense, 0BSD, Zlib, CC0-1.0.
- Weak copyleft (LGPL, MPL) and custom model licences are allowed case by case, with a note in the pull request.
- GPL, AGPL, SSPL and non-commercial licences are not allowed.
- Model weights are downloaded at install time and never committed. Their licences are listed too.
- PyMuPDF is dropped. PDF text and page rendering use pypdfium2 (Apache-2.0 or BSD-3-Clause), with pdfplumber (MIT, built on pdfminer.six) for font sizes used in heading detection.
- A licence allowlist check runs in CI (added with the backend and frontend skeletons) and a written licence audit (`docs/licence-audit.md`) is reviewed before the repository goes public. The project's own licence (Apache-2.0 or MIT) is chosen at publication.

Whether this PDF pair extracts well enough is a separate, hardware- and sample-dependent question, recorded as ADR 0015 (Proposed).

## Alternatives considered

- **Keep PyMuPDF and license the project AGPL**: restricts reuse and conflicts with the portfolio goal.
- **Buy a commercial PyMuPDF licence**: cost, and still not open.
- **Accept GPL or AGPL dependencies for specific features**: rejected as a general rule.

## Consequences

- Two smaller libraries instead of one, and somewhat slower layout analysis. pdfplumber runs only where heading detection needs it.
- EmbeddingGemma (a Gemma Terms of Use model) is usable as a fallback but is a custom licence and is noted in the audit rather than being the default.
- Every pull request that adds a dependency lists it and its licence.
