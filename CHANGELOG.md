# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Seven "find the document" questions in the public eval set (`find_document: true`, reworded from existing lookups in `demo/generate/questions.py`), so the embedding spike can report document-level Hit@1 on the public data.
- Access token in the page (`frontend/src/api/token.ts`, ADR 0016): the page takes the token from the address fragment `#token=...`, removes it from the address bar, keeps it in `sessionStorage` and sends it as a bearer header; without one it shows a notice with a password field and makes no request that would be refused. The API client, a session check on the dashboard and tests for each are included.
- Content-addressed blob store (`vault.storage.blobs`): hashing copy into `sha256/aa/bb/<hash>.<ext>`, read-only blobs, exact-duplicate handling that is safe under concurrent imports even under different extensions (a claim on the hash), an integrity error when a non-blob (a folder, a symbolic link or a junction) occupies a blob's place or one of its folders, a scan that reports every odd entry, start-up recovery (`clean_temp()` then `seal()`), `seal()` to restore read-only blobs after a crash, atomic publication with temporary-file cleanup, a size limit, `verify`, `scan` and `delete`.
- File validation (`vault.ingest.validate`): type by content, extension allowlist, size cap, and DOCX archive limits (checked before the zip directory is opened), and byte-order-marked text decoded and judged like any other, so spoofed, oversized and hostile files are refused before parsing.
- Local security and database foundation: Host, Origin and token checks on every request, a `current_user` seam, vault folder rules (never inside a Git repository), SQLite in WAL mode through SQLAlchemy Core, and an Alembic baseline migration.
- Frontend shell in `frontend/`: Vite, React, TypeScript and Mantine with Dashboard, Documents, Search, Ask and Settings placeholders, a light/dark toggle, a typed API client generated from the backend's OpenAPI schema and a backend health badge. The backend serves the built frontend from the same origin. ESLint, Prettier, `tsc`, Vitest, an npm licence check and `npm audit` run in CI.
- Synthetic demo corpus in `demo/corpus/` (44 fictional documents: text and scanned PDFs, DOCX in two versions, TXT, Markdown, CSV, PNG, JPG, duplicates), its reproducible generator in `demo/generate/`, a 60-question public eval set in `eval/public/questions.yaml` with a validation test, and `config.demo.toml`.
- Backend skeleton in `backend/`: FastAPI app factory with `GET /api/health`, TOML settings with environment overrides, logging that never records document content, pytest, Ruff, mypy, a Python licence check and `pip-audit` in CI.
- Repository foundation: ignore rules, repository safety check, pre-commit hooks, CI, templates and contributor docs.
- Architecture plan (revision 2) in `docs/architecture.md`, a template and 16 ADRs (13 Accepted, 3 Proposed pending Windows spikes), and an ADR structure check.

### Changed

- Dependabot no longer proposes a TypeScript major version (TypeScript 7 does not install next to `openapi-typescript` and `typescript-eslint`, so the npm group failed to resolve) or a `uv-build` bump (the build backend is pinned to the tested minor series and is bumped by hand).
