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
| Licence check | `uv run python ../scripts/check_python_licenses.py` |

## Settings

`config.toml` (copy `../config.example.toml`) is read from the current folder, or from the path in `AIVAULT_CONFIG`. Environment variables override it: `AIVAULT_SERVER__PORT=9000`. Unknown keys are an error. The server only accepts a loopback `server.host`.

## Logging

Use `vault.core.logging.log_event` and `timed`, never a bare `logger.info("... %s", text)`. Event names are fixed identifiers and fields are numbers, booleans or short identifiers, so document text cannot reach a log. Free text is replaced by `[redacted]` and exceptions are logged as type and location only.

## Dependencies

`uv.lock` is committed and CI installs with `--locked`. Runtime dependencies must be permissively licensed (CONTRIBUTING.md); `scripts/check_python_licenses.py` fails the build otherwise.
