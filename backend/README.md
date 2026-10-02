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

## File validation

`vault.ingest.validate.validate_file(path)` decides whether a file may be imported, before anything parses it: it must be a regular, non-empty file within the size cap, its extension must be on the allowlist (`pdf`, `docx`, `png`, `jpg`/`jpeg`, `txt`, `md`; PPTX, XLSX and CSV arrive with Phase 7), and its bytes must say the same thing as its name. The type comes from the content (PDF header, PNG and JPEG signatures, a zip with the DOCX parts, text with no NUL bytes), so a program renamed `report.pdf` or a picture renamed `notes.docx` is refused as `content_mismatch`. Text with a byte order mark (UTF-8, UTF-16, UTF-32) is decoded with the encoding the mark names and refused if it is invalid there, holds NUL or is mostly control characters, since the mark alone says nothing about the bytes after it. A DOCX is a zip, so without extracting anything the check also refuses encrypted entries, member names that climb out of the archive, too many entries, a too-large total and a compression bomb (`unsafe_archive`). The zip directory is bounded before `zipfile` is asked to open it, because `zipfile` reads the whole central directory and builds an object per record first: the end-of-directory record (and its ZIP64 form) is read from the last bytes of the file, found the way `zipfile` finds it, and a claim of more than the entry limit or more than 4 MiB of directory is refused unopened, and names legacy or password-protected Office files as such. The verdict carries a reason and a detected kind only, never the file's name or content, so it is safe to show or log. Only the first 8 KiB are read to recognise a file; a text file that turns binary later is caught at extraction. The sizes in a zip's directory can be understated by a hostile file, so the extractor must still bound what it reads. The check and the copy into the vault are separate reads: the import pipeline validates the copy it keeps, not the original path. The limits (`Limits`, 200 MiB, 2,000 entries, 4 MiB of directory, 1 GiB unpacked, ratio 200) are first choices to tune.

## Dependencies

`uv.lock` is committed and CI installs with `--locked`. Runtime dependencies must be permissively licensed (CONTRIBUTING.md); `scripts/check_python_licenses.py` fails the build otherwise.
