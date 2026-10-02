# Backend

The FastAPI process that will own the HTTP API, the background worker, the database and the vault folder (see `docs/architecture.md`). This is the skeleton: an app factory, `GET /api/health`, settings and logging. Security middleware and the database come next.

## Commands

Run from this folder. Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

| Task | Command |
| --- | --- |
| Install | `uv sync` |
| Run the server | `uv run python -m vault` |
| Tests with coverage | `uv run pytest` |
| Lint and format check | `uv run ruff check . && uv run ruff format --check .` |
| Type check | `uv run mypy` |
| Licence check | `uv run python ../scripts/check_python_licenses.py --ignore vault` |
| Export the API schema for the frontend | `uv run python -m vault.openapi ../frontend/openapi.json` |

## Settings

`config.toml` (copy `../config.example.toml`) is read from the current folder, or from the path in `AIVAULT_CONFIG`. Environment variables override it: `AIVAULT_SERVER__PORT=9000`. Unknown keys are an error. The server only accepts a loopback `server.host`. If `frontend/dist/index.html` exists (or `frontend.dist` points at a build), the server also serves the built frontend, so the app and the API share one origin; other GETs without a file extension return `index.html` for client-side routes, and unknown `/api/...` paths are 404.

## Logging

Use `vault.core.logging.log_event` and `timed`, never a bare `logger.info("... %s", text)`. Logging is deny by default: event names are literals like `ingest.chunk_done`, field names are `lower_snake_case`, and a field value is written only if it is a boolean, `None`, an enum member, a `Metric` or an `Identifier`. Every other value, including every plain `str`, `int` and `float`, is replaced by `[redacted]`. Create a `Metric` with `metric(...)` only from a number the application computed (a count, a size, a duration), never from a number read out of a document. Create an `Identifier` with `identifier(...)` only from an id the application generated (`doc_9f3a1c`, a content hash, a UUID), never from document text or a file name. A logger name is written only if it is an imported module's name (use `logging.getLogger(__name__)`), and the formatter never raises, because Python prints the raw message when a handler fails. Exceptions are logged as type and location only. Third-party loggers are deny by default too: only `uvicorn` and `uvicorn.error` messages are written, any other library's record is written as `message_omitted`, and the server runs with `access_log=False` because uvicorn's access log writes request paths and query strings.

## Blob store

`vault.storage.blobs.BlobStore` owns the vault's `blobs/` folder (ADR 0006). `put` copies a file's bytes to `sha256/aa/bb/<hash>.<ext>`, hashing as it copies, and marks it read-only; importing the same bytes again (under any extension) stores nothing and says so (`created=False`), and several imports racing for the same bytes still produce one blob and exactly one `created=True`. A copy is written to `blobs/.tmp/`, flushed, linked to its final name and only then marked read-only, so a crash or a failing source leaves a temporary file at worst (`clean_temp()` removes them at start-up), never a partial blob. `max_bytes` stops an oversized file while it is being read. The extension is lower-case letters and digits only and the hash is 64 lower-case hex characters, both checked with `fullmatch`, so nothing a caller passes can change the path. `verify` re-hashes a blob, `scan` lists the blobs and every entry under `sha256/` that is not one (for the integrity check), and `delete` is the one place a read-only original is removed. It never logs a file name or any content. A dedupe trusts that a stored blob is intact: finding damage is `verify`'s job, and repairing it is a restore from backup.

## Dependencies

`uv.lock` is committed and CI installs with `--locked`. Runtime dependencies must be permissively licensed (CONTRIBUTING.md); `scripts/check_python_licenses.py` fails the build otherwise.
