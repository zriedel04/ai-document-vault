# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Access token in the page (`frontend/src/api/token.ts`, ADR 0016): the page takes the token from the address fragment `#token=...`, removes it from the address bar, keeps it in `sessionStorage` and sends it as a bearer header; without one it shows a notice with a password field and makes no request that would be refused. The API client, a session check on the dashboard and tests for each are included.
- File validation (`vault.ingest.validate`): type by content, extension allowlist, size cap, and DOCX archive limits (checked before the zip directory is opened), and byte-order-marked text decoded and judged like any other, so spoofed, oversized and hostile files are refused before parsing.
- Local security and database foundation: Host, Origin and token checks on every request, a `current_user` seam, vault folder rules (never inside a Git repository), SQLite in WAL mode through SQLAlchemy Core, and an Alembic baseline migration.
- Frontend shell in `frontend/`: Vite, React, TypeScript and Mantine with Dashboard, Documents, Search, Ask and Settings placeholders, a light/dark toggle, a typed API client generated from the backend's OpenAPI schema and a backend health badge. The backend serves the built frontend from the same origin. ESLint, Prettier, `tsc`, Vitest, an npm licence check and `npm audit` run in CI.
- Synthetic demo corpus in `demo/corpus/` (44 fictional documents: text and scanned PDFs, DOCX in two versions, TXT, Markdown, CSV, PNG, JPG, duplicates), its reproducible generator in `demo/generate/`, a 60-question public eval set in `eval/public/questions.yaml` with a validation test, and `config.demo.toml`.
- Backend skeleton in `backend/`: FastAPI app factory with `GET /api/health`, TOML settings with environment overrides, logging that never records document content, pytest, Ruff, mypy, a Python licence check and `pip-audit` in CI.
- Repository foundation: ignore rules, repository safety check, pre-commit hooks, CI, templates and contributor docs.
- Architecture plan (revision 2) in `docs/architecture.md`, a template and 16 ADRs (13 Accepted, 3 Proposed pending Windows spikes), and an ADR structure check.
