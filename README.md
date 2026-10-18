# AI Document Vault

A local-first personal document library that acts as external memory for AI. When it is finished, you will import your documents, search them by keyword and meaning, and ask questions that are answered with citations pointing back to the exact page and passage. It runs on a Windows desktop as a local web app. The design keeps the vault on the PC. When you ask a question, the cloud model you picked receives only the question, the recent turns of that conversation and the passages retrieved to answer it. There is one planned exception, off unless you enable it: the opening section of a document may be sent once to get metadata suggestions.

This README explains what exists today, how it is built and why. A stated goal of the project is that every engineering decision is written down with its reasoning, so most sections link to the decision record behind them.

## Status

Pre-alpha. There is nothing to use day to day yet. The plan has phases 0 to 11 plus a backlog; phases 0 to 5 are the MVP ([plan, section 13](docs/architecture.md#13-development-phases)).

**Phase 0, foundations and spikes: nearly done** ([milestone](https://github.com/zriedel04/ai-document-vault/milestone/1)). Built:

- the backend skeleton ([`backend/`](backend/README.md)): FastAPI, settings, deny-by-default logging, SQLite with Alembic migrations, and request checks (`LocalSecurityMiddleware`). The `Host` header is checked on every HTTP request, and `Origin` on every request that can change something (any method but GET, HEAD and OPTIONS). The token is required on every `/api` path except `GET /api/health`; the static files of the UI need no token;
- the frontend shell ([`frontend/`](frontend/README.md)): navigation, light and dark mode, a typed API client generated from the backend's OpenAPI schema;
- the Windows launcher [`vault.ps1`](docs/launcher.md) and CI on Linux and Windows;
- a synthetic demo corpus (44 fictional documents) and a 60-question public evaluation set;
- the spike scripts for the four hardware-dependent questions. One has been run so far (sqlite-vec, [results below](#measured-so-far)); the embedding, OCR and PDF extraction spikes have yet to run on the owner's PC.

**Phase 1, vault storage and import: in progress.** Built so far:

- file validation by content (type, size, archive limits) before anything is parsed;
- the content-addressed blob store, with exact-duplicate handling that is safe under concurrent imports;
- the import of one file, recording a document, its version and where it came from, and an upload endpoint (`POST /api/imports/upload`);
- the persistent job queue and a worker pool that starts with the server (no processing stage has a handler yet);
- in the app, a document library with sorting and paging, a page for each document, **Download original**, and **Move to trash** and **Restore**.

Not built yet: an upload page in the app (uploads go through the API for now), permanent delete, text extraction, search, OCR, embeddings and Ask the Vault. The full list of what has changed is in the [changelog](CHANGELOG.md).

## Architecture in brief

One Python process owns everything: the HTTP API, the background workers, the databases and the vault folder. The browser is only a client.

```
Browser (React)  --HTTP/JSON-->  vault-server (FastAPI, 127.0.0.1:8765)
                                   |-- Host check on every request, Origin on writes,
                                   |   token on /api (except GET /api/health)
                                   |-- job queue + worker pool (same process)
                                   v
                                 vault folder (outside every Git repository)
                                   blobs/     originals, content-addressed, read-only
                                   vault.db   SQLite: documents, versions, jobs
                                   index.db   SQLite + sqlite-vec (planned, Phase 4)
```

| Decision | Why, in one line | Record |
| --- | --- | --- |
| Local-first | Extraction, OCR, search and embeddings will run locally. A cloud model will be used to answer questions, and will receive only the question, recent turns and the retrieved passages. Later, if you enable it, it may also receive a document's opening section once to suggest metadata. Every call will go through one egress gate, which refuses or drops passages from documents and labels marked local-only | [ADR 0001](docs/decisions/0001-local-first-design.md), [ADR 0010](docs/decisions/0010-cloud-models-receive-only-retrieved-context.md) |
| Local web app, React + FastAPI, bound to `127.0.0.1` | One process serves the API and the built UI to the browser; the server refuses to listen on anything but a loopback address | [ADR 0002](docs/decisions/0002-local-web-app.md) |
| Native Windows, no Docker or WSL2 | Docker and WSL2 add a virtual machine, slow file access and unreliable folder-change events, for a single-machine app that needs none of that | [ADR 0003](docs/decisions/0003-native-windows.md) |
| Two SQLite files instead of PostgreSQL | `vault.db` (WAL mode) holds what cannot be rebuilt; `index.db` holds vectors that can. FTS5 for keywords and sqlite-vec for vectors are planned for Phases 2 and 4 | [ADR 0004](docs/decisions/0004-sqlite-instead-of-postgresql.md) |
| In-process job queue instead of Redis and Celery | A `jobs` table and worker threads survive restarts and need no extra services | [ADR 0005](docs/decisions/0005-in-process-job-queue.md) |
| Content-addressed blobs | Each original is stored once under its SHA-256, read-only, so the same file imported twice is one document with two sources | [ADR 0006](docs/decisions/0006-content-addressed-storage.md) |
| Hybrid retrieval with Reciprocal Rank Fusion (planned) | Keyword search finds codes and names, vector search finds meaning; RRF merges the two lists by rank, because their scores are not comparable | [ADR 0007](docs/decisions/0007-hybrid-retrieval.md), [ADR 0008](docs/decisions/0008-reciprocal-rank-fusion.md) |
| No LangChain or LlamaIndex (planned) | The retrieval, prompt and provider code is what the owner most wants to inspect and explain, so it is written directly with thin adapters | [ADR 0009](docs/decisions/0009-no-ai-frameworks.md) |
| Citations resolved by the server (planned) | The model cites short labels; the server maps them to a page and checks the quote | [ADR 0011](docs/decisions/0011-citation-scheme.md) |
| Permissive licences only | So the repository can be public; this is why PyMuPDF (AGPL) is not used | [ADR 0012](docs/decisions/0012-licence-policy-and-pdf-libraries.md) |

The embedding model, the OCR engine and the PDF extraction approach are still open ([ADRs 0013 to 0015](docs/decisions/README.md), Proposed): they depend on hardware and are decided only by spikes on the owner's PC. The full plan is [`docs/architecture.md`](docs/architecture.md) (revision 2), and the [decision index](docs/decisions/README.md) lists every ADR with its status. Where an ADR and the plan disagree, the ADR is newer and wins.

## Privacy and safety design

The app manages personal documents, so privacy is designed in rather than added later.

- **Logging is deny by default.** A value reaches the log only if the code marks it as safe (an id the app generated, a count it computed); every other string and number is written as `[redacted]`, including one parsed from a document. Third-party log messages are omitted apart from the server's start-up and error lines, and exceptions are logged by type, never by message ([`backend/src/vault/core/logging.py`](backend/src/vault/core/logging.py)).
- **The API token never travels in a cookie or a URL the server sees** ([ADR 0016](docs/decisions/0016-local-api-token-delivery.md)). The launcher opens the browser with the token in the address fragment, which is never sent to a server; the page moves it into `sessionStorage`, removes it from the address bar and sends it only as a bearer header to its own origin. A cookie was tried first and dropped, because cookies are scoped by host and not by port, so another local program on `localhost` would have received it. The ADR also records the exposure that remains: while the browser starts, the token is on its command line.
- **The vault folder is outside every Git repository, and on Windows only you, the system and administrators can open it.** The server refuses to start with a vault folder inside any Git working tree or repository. On Windows it sets an access list on the vault folder and checks it, and the files below it, before going on. Access may be granted only to the current user, SYSTEM, the Builtin Administrators group and the Creator/Owner placeholders. An administrator account on the PC can therefore read the vault: an administrator can take ownership of any folder anyway, so leaving the group out would protect nothing. Any other account or group is refused.
- **A repository safety check** ([`scripts/check_repo_safety.py`](scripts/check_repo_safety.py)) blocks databases, documents, images, archives, keys, model files, local configuration and oversized files by name, and databases, documents, images and archives also by their content signature, whatever the file is called (the synthetic fixture folders may hold documents, images and archives). It checks staged files, every tracked file and the whole Git history, in pre-commit and in CI, beside gitleaks.
- **Test data is synthetic only.** Tests, CI and the demo use the generated corpus in [`demo/corpus/`](demo/generate/README.md) and the public questions in [`eval/public/`](eval/README.md). Private questions stay in the vault folder.

The rules for contributors are in [CONTRIBUTING.md](CONTRIBUTING.md), and how to report a vulnerability is in [SECURITY.md](SECURITY.md).

## How AI tools are used

This project is built with AI coding tools, openly, under a fixed process ([`AGENTS.md`](AGENTS.md)):

1. **Claude Code implements** one focused change on a branch and opens a pull request that a reviewer can follow without the chat history.
2. Tests, linting, type checks, the safety check and CI must pass.
3. **OpenAI Codex reviews** the pull request independently. Each finding is checked on its merits and sorted: fixed now, fixed a better way, out of scope (a follow-up issue), or not valid (with evidence). Nothing is applied blindly.
4. **The owner merges**, only after explicitly approving. No AI tool pushes to `main` or merges.

When a kind of mistake comes up in review more than once, it is recorded in the [Lessons from review](CLAUDE.md#lessons-from-review) list in `CLAUDE.md` (eleven entries so far, such as "fix the class, not the instance" and "assume the bypass"), so that later changes are checked against it before they are pushed.

## Measured so far

Only one spike has been run: [Spike 01, sqlite-vec](docs/spikes/01-sqlite-vec.md), on the owner's Windows 11 PC on 2026-10-17, with random synthetic vectors of 1,024 dimensions.

| Vectors | Database size | Exact search, p50 | Exact search, p95 | Inserts per second |
| --- | --- | --- | --- | --- |
| 10,000 | 40.3 MiB | 28.1 ms | 32.4 ms | about 2,480 |
| 100,000 | 394.6 MiB | 300.5 ms | 317.7 ms | about 2,000 |

The extension loaded, and its exact search gave the same ranked ten neighbours as a plain-Python search on 5 of 5 probe queries. Latency grows about linearly with the number of vectors. That run used SQLite's rollback journal rather than the WAL mode the app uses, so the insert rates are to be measured again; the search figures are not expected to change. The recommended conclusion (keep ADR 0004; exact search slows past roughly 100k chunks, sooner than the plan estimated) is awaiting the owner's confirmation. How spikes are run and recorded: [`docs/spikes/`](docs/spikes/README.md).

## Quick start

You need Windows, [uv](https://docs.astral.sh/uv/) and Node.js 22.13 or newer on an even-numbered line (22.13+, 24 or 26+, the range `frontend/package.json` requires). The backend runs on Python 3.12 only (`backend/pyproject.toml`); uv installs it if it is missing. Copy `config.example.toml` to `backend\config.toml`, set `vault.path` to an absolute folder outside every Git repository (the server refuses to start otherwise), then run `.\vault.ps1 install` and `.\vault.ps1 run`. The launcher builds the app, starts the server and opens the browser; `.\vault.ps1 stop` stops it and `.\vault.ps1 dev` runs the development servers. Details: [docs/launcher.md](docs/launcher.md).

## Repository layout

| Path | Contents |
| --- | --- |
| [`backend/`](backend/README.md) | The FastAPI server (`src/vault/`: api, core, db, ingest, jobs, storage) and its tests |
| [`frontend/`](frontend/README.md) | The React app (Vite, TypeScript, Mantine, TanStack Router) |
| [`docs/`](docs/architecture.md) | The architecture plan, ADRs in `decisions/`, spike results in `spikes/`, the launcher guide |
| [`demo/`](demo/generate/README.md) | The synthetic corpus and its reproducible generator |
| [`eval/`](eval/README.md) | The public evaluation questions |
| `scripts/` | The safety, ADR and licence checks, the spike scripts and their tests |
| `vault.ps1` | The Windows launcher |

## Running the checks

| Task | Command |
| --- | --- |
| Safety check tests | `python -m unittest discover -s scripts/tests -v` |
| Safety check, files and history | `python scripts/check_repo_safety.py --history` |
| ADR structure check | `python scripts/check_adrs.py` |
| Launcher tests (Windows) | `powershell -NoProfile -File scripts\tests\launcher.tests.ps1` |
| All hooks | `pre-commit run --all-files` |
| Backend tests, lint and types (in `backend/`) | `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` |
| Frontend checks (in `frontend/`) | `npm test`, `npm run lint`, `npm run format:check`, `npm run build` |
| Python and npm licence checks | `uv run python ../scripts/check_python_licenses.py --ignore vault` (in `backend/`), `python scripts/check_npm_licenses.py` |
| Demo corpus tests (in `demo/generate/`) | `uv run python -m unittest discover -s tests -v` |
| Rebuild the corpus and eval set (in `demo/generate/`) | `uv run python build.py` (`--check` fails if the committed files differ) |

This table mirrors the one in [`AGENTS.md`](AGENTS.md), which is the one kept current.

## Licence

[Apache License 2.0](LICENSE). Why this licence: [ADR 0017](docs/decisions/0017-project-licence.md).
