# The launcher (`vault.ps1`)

One PowerShell script at the repository root installs, starts and stops the app on Windows. It runs in Windows PowerShell 5.1 (the one every Windows 11 PC has) and in PowerShell 7. The full command table is in `docs/architecture.md` (section 4); this page says what the script does today and where it differs from that table.

| Command | What it does |
| --- | --- |
| `.\vault.ps1 install` | `uv sync --locked` in `backend/`, `npm ci` and `npm run build` in `frontend/`. Stops with the install command if `uv` or Node is missing. |
| `.\vault.ps1 run` | Starts the server hidden in the background, waits until `/api/health` answers, reads the token from the vault folder and opens the browser at `http://127.0.0.1:8765/#token=<token>` (ADR 0016). Run again while the server is up, it only opens the browser. `-NoBrowser` skips that. |
| `.\vault.ps1 stop` | Stops the server that `run` started. |
| `.\vault.ps1 dev` | Starts the server like `run` and the Vite dev server (`http://127.0.0.1:5173/`) in the window; Ctrl+C stops both. |

## Settings

The server reads `backend\config.toml` (copy `config.example.toml` there and set `vault.path`), or the file named by the `AIVAULT_CONFIG` environment variable. The launcher never parses that file: it asks the backend (`vault.core.config.load_settings`) for the vault folder, host and port, so it cannot disagree with the server. Without a vault folder it stops with a message, because the app has no database and no token then.

## State and logs

The launcher keeps a process id and the server's output in `.run/` (git-ignored). The record holds the process id and the process's start time, so `stop` never ends a process that only reuses the number. The server's console output goes to `.run\server.out.log` and `.run\server.err.log`; the backend's logging holds no document content (see `backend/README.md`).

## The token

The token is never printed. It is in the address handed to the browser, which makes it visible to other local programs for a moment as part of the browser's command line; ADR 0016 accepts that limit and describes the alternative.

## Differences from the plan's table

- `install` does not install `uv` or Node. It stops and prints the `winget` command. Piping a downloaded script into PowerShell is not something a launcher should do silently.
- `install` does not download the embedding and OCR models. Which models are used depends on the spikes that are still to be run (ADRs 0013 to 0015).
- `stop` ends the process. It is not yet graceful, because there are no background jobs to pause; a shutdown request with the token comes with the job queue.
- `dev` does not reload the backend: stop and start it after a backend change. It expects the server on port 8765, the port the Vite proxy in `frontend/vite.config.ts` is fixed to.
- `configure`, `update`, `backup` and `demo` arrive with the features they serve.

## Tests

`scripts/tests/launcher.tests.ps1` checks the token shape (including a trailing newline, as lesson 4 in `CLAUDE.md` asks), the address, the process record, the token file and how the script is called, in both PowerShell editions. The `Launcher (Windows)` CI job also runs `install`, `run`, a second `run`, `stop` and a refused start without a vault folder against a real server, in both editions, using a throwaway vault in the runner's temporary folder. Opening the browser is the one step CI cannot show.
