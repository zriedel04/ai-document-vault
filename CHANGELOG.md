# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- File validation (`vault.ingest.validate`): type by content, extension allowlist, size cap, and DOCX archive limits (checked before the zip directory is opened), and byte-order-marked text decoded and judged like any other, so spoofed, oversized and hostile files are refused before parsing.
- Frontend shell in `frontend/`: Vite, React, TypeScript and Mantine with Dashboard, Documents, Search, Ask and Settings placeholders, a light/dark toggle, a typed API client generated from the backend's OpenAPI schema and a backend health badge. The backend serves the built frontend from the same origin. ESLint, Prettier, `tsc`, Vitest, an npm licence check and `npm audit` run in CI.
- Backend skeleton in `backend/`: FastAPI app factory with `GET /api/health`, TOML settings with environment overrides, logging that never records document content, pytest, Ruff, mypy, a Python licence check and `pip-audit` in CI.
- Repository foundation: ignore rules, repository safety check, pre-commit hooks, CI, templates and contributor docs.
- Architecture plan (revision 2) in `docs/architecture.md`, a template and 15 ADRs (12 Accepted, 3 Proposed pending Windows spikes), and an ADR structure check.
