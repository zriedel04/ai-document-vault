# Changelog

All notable changes are recorded here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Phase 0 spike scripts in `scripts/spikes/` (sqlite-vec, embedding model, OCR engine, PDF extraction) that record metrics only, with unit tests that use fakes and a CI job. Nothing has been measured yet; the spikes run on the Windows PC.
- Backend skeleton in `backend/`: FastAPI app factory with `GET /api/health`, TOML settings with environment overrides, logging that never records document content, pytest, Ruff, mypy, a Python licence check and `pip-audit` in CI.
- Repository foundation: ignore rules, repository safety check, pre-commit hooks, CI, templates and contributor docs.
- Architecture plan (revision 2) in `docs/architecture.md`, a template and 15 ADRs (12 Accepted, 3 Proposed pending Windows spikes), and an ADR structure check.
