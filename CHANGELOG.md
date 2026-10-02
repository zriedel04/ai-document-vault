# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Phase 0 spike scripts in `scripts/spikes/` (sqlite-vec, embedding model, OCR engine, PDF extraction) that record metrics only, with unit tests that use fakes and a CI job. Nothing has been measured yet; the spikes run on the Windows PC.
- Local security and database foundation: Host, Origin and token checks on every request, a `current_user` seam, vault folder rules (never inside a Git repository), SQLite in WAL mode through SQLAlchemy Core, and an Alembic baseline migration.
- Frontend shell in `frontend/`: Vite, React, TypeScript and Mantine with Dashboard, Documents, Search, Ask and Settings placeholders, a light/dark toggle, a typed API client generated from the backend's OpenAPI schema and a backend health badge. The backend serves the built frontend from the same origin. ESLint, Prettier, `tsc`, Vitest, an npm licence check and `npm audit` run in CI.
- Backend skeleton in `backend/`: FastAPI app factory with `GET /api/health`, TOML settings with environment overrides, logging that never records document content, pytest, Ruff, mypy, a Python licence check and `pip-audit` in CI.
- Repository foundation: ignore rules, repository safety check, pre-commit hooks, CI, templates and contributor docs.
- Architecture plan (revision 2) in `docs/architecture.md`, a template and 15 ADRs (12 Accepted, 3 Proposed pending Windows spikes), and an ADR structure check.
