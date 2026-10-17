# Licence audit

- Date: 2026-10-15
- Scope: the repository at the time it was made public (main after PR #33) and the project licence (ADR 0017, PR #34)
- Policy: [CONTRIBUTING.md](../CONTRIBUTING.md#licences) and [ADR 0012](decisions/0012-licence-policy-and-pdf-libraries.md)

The plan asked for this audit, and for the generated [third-party notices](../THIRD_PARTY_NOTICES.md), to be reviewed before the repository went public. The repository went public on 2026-10-15, before either existed, so this audit was written afterwards. It found nothing that the publication exposed against the policy.

## Summary

| Area | Packages | Result |
| --- | --- | --- |
| Backend runtime (Python) | 20 | All on the allowed list (MIT, BSD-3-Clause, Apache-2.0, PSF-2.0) |
| Frontend runtime (npm) | 39 | All on the allowed list (MIT, 0BSD, Unlicense, MIT or CC0-1.0) |
| Backend development tools (Python) | 37 in the environment, runtime included | Allowed, plus one reviewed exception (pathspec, MPL-2.0) |
| Frontend development tools (npm) | 318 in the lockfile, runtime included | Allowed, plus reviewed exceptions (below) |
| Demo corpus generator (Python) | 12 in the environment | Allowed, plus one reviewed exception (Pillow, MIT-CMU) |
| Spike extras (Python, Windows only) | not installed by CI | Checked when the spikes were added; see below |
| Models and external programs | none committed | Downloaded or installed on the owner's PC; licences listed below |
| Committed assets | demo corpus, icons | Generated here or covered by a package licence |
| GPL, AGPL, SSPL or non-commercial | 0 | None found anywhere |

## Method

- **Python**: `scripts/check_python_licenses.py` reads every installed package's licence metadata (the SPDX expression first, then classifiers, then a short `License` field) and fails on anything not allowed or not readable. CI runs it on the full backend environment, on the runtime-only environment with `--strict` (no reviewed exceptions), and on the demo generator's environment.
- **npm**: `scripts/check_npm_licenses.py` reads the licence of every package in `frontend/package-lock.json`. CI runs it on everything and, with `--strict`, on the runtime packages (those not marked `dev`).
- **Notices**: `scripts/third_party_notices.py write` produced [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) from the runtime packages' own licence files. CI checks that its package list and each package's licence texts still match the lockfile and the runtime environment (`check npm`, `check python`). The Python check and the licence gate run in a runtime-only environment on Linux and on Windows, because a dependency can add a package only on Windows or ship different licence files in its Windows wheel; no runtime dependency does so today. If one ever does, the two checks cannot both pass with one notices file, because each compares the exact set of packages it sees, so the PR that adds such a dependency must also teach the script platform-specific entries. Versions are not compared, so an update that keeps the licence text needs no regeneration.
- **Manual review**: the reviewed exceptions, the spike extras, models, external programs and the committed assets, which no script reads.

The checks read package metadata. They do not open the source to look for files under a different licence than the package declares; that is the usual limit of an automated audit.

## Runtime packages

These are what a user of the app runs. The full list with licence texts is in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

- **Python (20)**: alembic, annotated-doc, annotated-types, anyio, click, fastapi, h11, idna, Mako, MarkupSafe, opentelemetry-api, pydantic, pydantic-core, pydantic-settings, python-dotenv, SQLAlchemy, starlette, typing-extensions, typing-inspection, uvicorn. All MIT, BSD-3-Clause, Apache-2.0 (opentelemetry-api) or PSF-2.0 (typing-extensions).
- **npm (39)**: React, Mantine, TanStack Router, Floating UI, Tabler icons, openapi-fetch and their dependencies. All MIT, except `tslib` (0BSD), `isbot` (Unlicense) and `type-fest` (MIT or CC0-1.0).
- `react-remove-scroll-bar` declares MIT but its package ships no licence file. The notices carry the MIT text its author later added to the upstream repository (`scripts/notices/`, commit 7301c16), and say where it comes from. A runtime package with no licence file and no such reviewed text fails the notices check.

The built frontend bundles npm code into its own files. MIT and the other licences above ask for their notice to be kept with copies of the code, which `THIRD_PARTY_NOTICES.md` does for anyone redistributing a build. Python packages are not bundled: `uv` installs them from PyPI with their own licence files.

## Reviewed exceptions (development only)

Each is listed with its reason in the `REVIEWED` table of the check script, and none is allowed in the runtime check.

| Package | Licence | Why it is acceptable |
| --- | --- | --- |
| pathspec (Python, through mypy) | MPL-2.0 | File-level copyleft that applies only if pathspec itself is changed or redistributed. Type checker dependency, not shipped |
| Pillow (Python, demo generator) | MIT-CMU | Permissive (HPND family). Renders the synthetic demo images; not shipped |
| lightningcss (npm, through Vite) | MPL-2.0 | As pathspec. Build tool; its code is not in the built app |
| caniuse-lite (npm, through Vite) | CC-BY-4.0 | Browser-support data used at build time; not in the built app |
| @csstools/* (npm, through jsdom) | MIT-0 | MIT without the attribution requirement. Test tooling |
| lru-cache, minimatch (npm, through ESLint) | BlueOak-1.0.0 | Permissive, ISC family. Lint and test tooling |

Adding MIT-0, MIT-CMU and BlueOak-1.0.0 to the allowed list is an open question for the owner; until then they stay case by case.

## Spike extras

The Phase 0 spike scripts (`scripts/spikes/`) have optional extras that run only on the owner's Windows PC and are never installed by CI: `vec` (sqlite-vec), `embedding` (sentence-transformers, pypdfium2, and through them PyTorch and Hugging Face libraries), `ocr` (rapidocr, onnxruntime) and `pdf` (pypdfium2, pdfplumber).

When the spikes were added (PR #16) their licences were checked over the Windows wheels: 64 packages, none GPL or AGPL. `certifi` and `tqdm` are MPL-2.0 (case by case, used unchanged). This audit did not reinstall them, because PyTorch and the OCR models are several gigabytes; whichever candidate wins a spike becomes a backend dependency and then passes the CI checks above like any other.

One point matters for ADR 0014 (OCR engine): `rapidocr` requires `opencv-python`, whose wheels bundle FFmpeg under LGPL-2.1. LGPL is allowed case by case. If RapidOCR wins, the dependency PR has to record the review, and redistributing a build that bundles OpenCV would need the LGPL notice and the right to replace the library.

## Models and external programs

None are committed. Each is downloaded or installed on the owner's PC.

| Component | Licence | Note |
| --- | --- | --- |
| Qwen3-Embedding-0.6B (embedding, ADR 0013 candidate) | Apache-2.0 | Weights downloaded at first use |
| bge-reranker-v2-m3 (planned reranker) | Apache-2.0 | Not used yet |
| EmbeddingGemma-300M (fallback candidate) | Gemma Terms of Use | Custom licence with use restrictions. Not the default; needs the owner's decision before it ships |
| RapidOCR models (PP-OCR) | Apache-2.0 | Downloaded by rapidocr |
| Tesseract (OCR candidate) | Apache-2.0 | A separate program the owner installs; not bundled |

## Committed assets

- **Demo corpus** (`demo/corpus/`): every document is fictional and generated by `demo/generate/`. Text PDFs use the PDF standard fonts (Helvetica), which are referenced, not embedded. The scanned images are drawn with Pillow's bundled default font, Aileron Regular, which its designer (dotcolon.net) releases under CC0.
- **Icons in the frontend** come from `@tabler/icons-react` (MIT), covered in the notices.
- No other fonts, images or third-party data are committed.

## Project licence

Apache-2.0 ([ADR 0017](decisions/0017-project-licence.md)). Every licence above is compatible with distributing the project under it.

## Keeping this current

- A new, removed or relicensed runtime dependency, or one whose licence text changed, fails CI until `THIRD_PARTY_NOTICES.md` is regenerated; a disallowed licence also fails the licence checks.
- A new reviewed exception goes in the check script's `REVIEWED` table, with its reason, and in the table above.
- Review this audit again before the first tagged release and whenever a spike result turns a candidate into a dependency.
