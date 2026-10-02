# Backend

The FastAPI process that will own the HTTP API, the background worker, the database and the vault folder (see `docs/architecture.md`). It has an app factory, request protections, the vault folder rules, the database with its migrations, settings and logging. Document handling comes later.

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

`config.toml` (copy `../config.example.toml`) is read from the current folder, or from the path in `AIVAULT_CONFIG`. Environment variables override it: `AIVAULT_SERVER__PORT=9000`. Unknown keys are an error. The server only accepts `127.0.0.1`, `localhost` or `::1` as `server.host`, the three spellings the Host check accepts. If `frontend/dist/index.html` exists (or `frontend.dist` points at a build), the server also serves the built frontend, so the app and the API share one origin; other GETs without a file extension return `index.html` for client-side routes, and unknown `/api/...` paths are 404.

## Security

Every request passes through `vault.core.security.LocalSecurityMiddleware`, which is deny by default. The server binds to loopback only, but that does not stop other websites or other accounts on the same PC, so:

- `Host` must be `localhost`, `127.0.0.1` or `[::1]`, with an optional port (this stops DNS rebinding).
- Any method except GET, HEAD and OPTIONS needs an `Origin` equal to `http://` plus the `Host` value.
- Every `/api` path except `GET /api/health` and `POST /api/session` needs the install's token, as `Authorization: Bearer <token>` or as the `vault_session` cookie. `POST /api/session` checks the bearer token and sets that cookie (HttpOnly, `SameSite=Strict`; a browser keeps it per host name, so open the app at one address, such as `http://127.0.0.1:8765`, and stay on it). `GET /api/session` returns the signed-in user.
- There is no CORS configuration: no other origin is ever allowed. A refusal is a 403 (Host or Origin) or 401 (token) with a fixed body, and the log line carries the reason only, never a header value.

The token is the file `local-token` in the vault folder, created on first start with mode 0600. On Windows `chmod` does nothing useful and a folder under a drive root lets every account on the PC read it, so `vault.storage.acl` makes the vault folder owner-only (it removes the inherited entries and grants only the current user, through PowerShell), reads the access list back and refuses to start unless every entry that grants access names your account, SYSTEM or the Administrators group (an administrator can take ownership of any folder anyway). A second account or a domain group named in the list, as well as Users, Authenticated Users or Everyone, makes the folder not private. The folder's owner has to be your account, SYSTEM or Administrators too, because an owner can rewrite the list. An existing vault is also checked file by file for what the program trusts at start-up (the token, the database and its side files, and the four subfolders): a file moved in from another folder keeps its own access list, so one that is open stops the start and is named in the error, without being changed. Files deeper in the subfolders are not checked at start-up, because the app writes them itself into folders that are already private; restore a vault by copying it in, not by moving it. A folder that already has files and is not private is not changed; the error says how to fix it. A file that is not a well-formed token stops the start-up: delete it to get a new one. Without a vault folder (demo mode, tests) the token is random for that process and nobody holds it, so only the open endpoints work. Every endpoint takes the user from `Depends(current_user)`, which today returns the one local user; real login would change that function only.

## Vault folder and database

`vault.path` must be an absolute folder outside every Git repository: a path whose resolved location has a `.git` file or folder (or is a bare repository) at or above it is refused before anything is created, so `..` and symbolic links cannot get around it. `ensure_vault` then creates `blobs/`, `derived/`, `logs/` and `backups/`. The start-up also migrates `vault.db` with Alembic (`vault.db.migrate.upgrade_to_latest`) and opens it through `vault.db.engine.create_vault_engine`: WAL mode (an error if the file system cannot do it), `synchronous=NORMAL`, foreign keys on and a busy timeout, on every connection. Table definitions are in `vault/db/schema.py` and each change to them needs a revision in `vault/db/migrations/versions/`; a test fails if the two disagree. `env.py` does not call `fileConfig`, because that would replace the safe logging.

## Logging

Use `vault.core.logging.log_event` and `timed`, never a bare `logger.info("... %s", text)`. Logging is deny by default: event names are literals like `ingest.chunk_done`, field names are `lower_snake_case`, and a field value is written only if it is a boolean, `None`, an enum member, a `Metric` or an `Identifier`. Every other value, including every plain `str`, `int` and `float`, is replaced by `[redacted]`. Create a `Metric` with `metric(...)` only from a number the application computed (a count, a size, a duration), never from a number read out of a document. Create an `Identifier` with `identifier(...)` only from an id the application generated (`doc_9f3a1c`, a content hash, a UUID), never from document text or a file name. A logger name is written only if it is an imported module's name (use `logging.getLogger(__name__)`), and the formatter never raises, because Python prints the raw message when a handler fails. Exceptions are logged as type and location only. Third-party loggers are deny by default too: only `uvicorn` and `uvicorn.error` messages are written, any other library's record is written as `message_omitted`, and the server runs with `access_log=False` because uvicorn's access log writes request paths and query strings.

## Dependencies

`uv.lock` is committed and CI installs with `--locked`. Runtime dependencies must be permissively licensed (CONTRIBUTING.md); `scripts/check_python_licenses.py` fails the build otherwise.
