# The launcher (`vault.ps1`)

One PowerShell script at the repository root installs, starts and stops the app on Windows. It runs in Windows PowerShell 5.1 (the one every Windows 11 PC has) and in PowerShell 7. The full command table is in `docs/architecture.md` (section 4); this page says what the script does today and where it differs from that table.

| Command | What it does |
| --- | --- |
| `.\vault.ps1 install` | `uv sync --locked` in `backend/`, `npm ci` and `npm run build` in `frontend/`. Stops with the install command if `uv` or Node is missing. |
| `.\vault.ps1 run` | Starts the server hidden in the background, waits until `/api/health` answers with the launch id it was started with (below), reads the token from the vault folder and opens the browser at `http://127.0.0.1:8765/#token=<token>` (ADR 0016). Run again while the server is up, it only opens the browser, at the address and with the token of the server that is up (the settings it was started with are in the record in `.run/`), and says so if `config.toml` has changed since. `-NoBrowser` skips that. |
| `.\vault.ps1 stop` | Stops the server that `run` started. |
| `.\vault.ps1 dev` | Starts the server like `run` and the Vite dev server (`http://127.0.0.1:5173/`) in the window, and opens the browser once Vite answers with its own launch id; Ctrl+C stops both. Refuses to start if something already answers on port 5173. |
| `.\vault.ps1 check` | Checks the vault without changing it (plan section 17): runs `python -m vault.check` in `backend/`, which reads the same settings as the server. Exits with 0 when nothing is wrong and 1 when the check found a problem or could not run. See below. |
| `.\vault.ps1 backup -To <folder>` | Snapshots `vault.db` and copies the original files that are not yet in the folder (below). Safe while the app runs; it does not start or stop the server. |

## Settings

The server reads `backend\config.toml` (copy `config.example.toml` there and set `vault.path`), or the file named by the `AIVAULT_CONFIG` environment variable. The launcher never parses that file: it asks the backend (`vault.core.config.load_settings`) for the vault folder, host and port, so it cannot disagree with the server. Without a vault folder it stops with a message, because the app has no database and no token then.

## State and logs

The launcher keeps a process id and the server's output in `.run/` (git-ignored). The record holds the process id and the process's start time, so `stop` never ends a process that only reuses the number, and the host, port and vault folder the server was started with. The server's console output goes to `.run\server.out.log` and `.run\server.err.log`; the backend's logging holds no document content (see `backend/README.md`).

The record is UTF-8 without a byte order mark, written and read the same way in both editions, because the vault folder in it may have an accented letter in a Windows user name (Windows PowerShell's `-Encoding ASCII` would turn it into `?`). The backend reports the folder to the launcher as JSON with escaped letters, so the console's code page never touches it.

## Starting is serialised and checks the port

`run`, `stop` and `dev` (while it starts the server) hold one lock per checkout (a named mutex over the `.run` folder) around "is it running, start it, write the record". Two commands started together therefore take turns, and the second finds the server the first started, instead of both starting one and the loser deleting the winner's record. A command that cannot get the lock within two minutes stops with a plain message. Windows releases the lock of a process that ended, and the next command takes it over.

Before it starts anything, the launcher also tries to connect to the configured address (and, for `dev`, to Vite's port 5173), and refuses if something answers, so that the usual case (another copy of the app, another dev server) gets a plain message.

That check alone cannot keep the token from another program: one can take the port after it, and the server binds only after its start-up checks, which can take minutes. So readiness is not "something answers". The launcher starts the server, and Vite for `dev`, with a fresh random launch id in the environment variable `VAULT_LAUNCH_ID`; `/api/health` (`backend/src/vault/api/health.py`) and Vite's `/__vault/launch` (`frontend/vite.config.ts`) answer with it, and the browser is opened, with the token, only when the answer holds the same id. No other program, and no other Windows account, can read the environment of the process the launcher started. An answer with any other id, or none, stops the start with a message and ends the process that was started. The id is not a secret once the server is up: anyone may ask `/api/health` for it, and the next start uses a new one.

If start-up does not complete for any reason (the record cannot be written, the server exits or does not answer within 20 minutes, Ctrl+C), the launcher ends the server it started, so no server runs that `stop` cannot find. The wait is long because the first start of an existing vault checks the access list of every file below it, which the backend allows 15 minutes; after ten seconds the launcher says that it is still starting. A server that exits is noticed at once. A record is removed only if it still names the process that was ended (its id and start time): `stop` can end the server of a `dev` that is still open, `run` can then start another one, and `dev` closing later must not forget that one. `dev` ends its server under the lock, like every other change to the record. `stop` removes the record only once the process has actually exited: if Windows refuses to end it, or it is still there after ten seconds, `stop` says so, exits with an error and keeps the record so that it can be run again.

## The integrity check

`check` runs SQLite's `PRAGMA integrity_check` and `PRAGMA foreign_key_check` on `vault.db` (and on `index.db` if it exists), re-hashes every blob, reports every entry in the blob store that is not a blob (links, hard links, one hash under several names, anything else out of place), and compares the database with the store: versions whose blob is missing, blob rows that no version uses and whose file is gone, blobs that no row records and blobs that no version uses. The report gives the count and kind of each problem and, for blob problems, the content hash; it never prints a file name, a path or a document name, and every message is fixed text. The backend returns 0 (nothing wrong), 1 (a problem was found) or 2 (the check could not run: no settings, no vault folder); the launcher exits with 1 for either failure, with a message that says which.

It only reads: the databases are opened read-only, and nothing is migrated, sealed, moved to `blobs/orphans/` or deleted. Repairing what it finds is a restore from backup or the owner's decision. When `vault.db` has no SQLite side files, opening it creates an empty `-wal` and the `-shm` index, as any SQLite reader of a WAL database does; neither holds database pages.

It can run while the server runs. It takes no launcher lock and touches no record, the database is in WAL mode, so a reader sees one committed snapshot and neither side blocks the other, and blobs are only read. The store and the database cannot be read at the same instant, so every difference between them and every odd entry is looked at once more after the first pass, and one that has gone (an import that finished in between) is not reported. An import that is still copying when the check ends can still show as a blob without a row: run the check again, or with the server stopped, to tell that from damage.
## Backup

`backup -To <folder>` runs `python -m vault.backup` in `backend/` with the backend's own settings, so it backs up the vault the server uses. The folder is resolved against the current PowerShell location (a relative `-To` means the folder you are in, not `backend/`) and goes to Python in the environment variable `VAULT_BACKUP_TO`, never on its command line: Windows PowerShell 5.1 does not escape a trailing backslash inside the quotes it adds around a path with a space, so `"E:\My Backups\"` would reach Python with a stray quote. The backend's lines are passed on as they are, and the launcher stops with the exit code if the backup does not complete.

What ends up in the folder:

```
<folder>\
  backup.lock                          held while a backup runs
  blobs\sha256\aa\bb\<hash>.<ext>        every original file, copied once, read-only
  snapshots\2026-10-17T143005Z\         one folder per backup, named by the UTC time
    vault.db                           consistent copy of the database, read-only
    config.toml                        the settings file, read-only
```

The database snapshot is SQLite's `VACUUM INTO`, consistent while the app writes. Only original files the snapshot lists and the folder does not hold yet are copied, each to a temporary name, checked against its hash and then given its name, so an interrupted backup or a full disk never leaves a partial file under a final name. `index.db`, `derived\`, `logs\` and `local-token` are left out (rebuildable, or a credential the server makes again). The command prints counts and the snapshot's name only, never a file name or the folder. It refuses a folder inside the vault, inside a Git repository or reached through a link or junction, and never overwrites a snapshot; on Windows it makes a new folder private to your account, checks every folder and file below it after writing, and warns if anything in the backup can be read by other accounts (use a BitLocker-encrypted drive). Old snapshots are not deleted yet (the plan's 14 daily and 12 monthly): delete them by hand. A snapshot folder without `vault.db` is a backup that was interrupted and can be deleted. Details: `backend/README.md`, section Backup.

## The token

The token is never printed. It is in the address handed to the browser, which makes it visible to other local programs for a moment as part of the browser's command line; ADR 0016 accepts that limit and describes the alternative.

## Differences from the plan's table

- `install` does not install `uv` or Node. It stops and prints the `winget` command. Piping a downloaded script into PowerShell is not something a launcher should do silently.
- `install` does not download the embedding and OCR models. Which models are used depends on the spikes that are still to be run (ADRs 0013 to 0015).
- `stop` ends the process. It is not yet graceful, because there are no background jobs to pause; a shutdown request with the token comes with the job queue.
- `dev` does not reload the backend: stop and start it after a backend change. It needs the server on `127.0.0.1:8765`, the address the Vite proxy in `frontend/vite.config.ts` is fixed to, and refuses any other host or port (`localhost` can resolve to `::1`).
- `configure`, `update` and `demo` arrive with the features they serve.
- `check` (section 17) is here before Phase 10, where the plan lists it, because its parts (`BlobStore.verify` and `scan`) already exist. It checks the vault the settings name now, not the one a running server was started with.
- `backup` keeps every snapshot; the plan's retention (14 daily and 12 monthly) and the scheduled daily run are not part of it yet.

## Tests

`scripts/tests/launcher.tests.ps1` checks the token shape (including a trailing newline, as lesson 4 in `CLAUDE.md` asks), the address, the process record (including a folder name with letters outside ASCII), the port check, the lock, `stop` when the server cannot be ended, a start-up that fails after the server has started, a record that names another process, the launch id (an answer with the same id, another one, none, or not JSON, a process that exits, one that never answers), the token file, how `check` turns the backend's exit code into its own (with a stand-in for Python that records its arguments, in a backend folder of its own), `backup` (no folder, a relative folder, a folder with a space and a trailing backslash, the folder kept off the backend's command line and out of the environment afterwards, a backend that fails, run against an in-process stand-in for Python in a backend folder of its own) and how the script is called, in both PowerShell editions. The `Launcher (Windows)` CI job also runs `install`, `run`, a second `run`, `stop` and a refused start without a vault folder, a refused start on a port something else holds, two `run` commands started together, `dev` with a taken Vite port, `dev` ending its server when Vite ends, and `dev` leaving alone a server that `run` started after `stop` had ended dev's own, and `check` passing the vault (also beside the running server) and failing it, without naming the file, once a stray file is put in the blob store, against a real server (in a vault folder with an accented letter in its name, so the record is read back by the other edition as well), in both editions, using a throwaway vault in the runner's temporary folder. The same job runs `backup` in both editions while the server runs, into a folder with a space, an accented letter and a trailing backslash, checks that two snapshots exist and that the folder is never printed, and checks that a folder inside the vault is refused. Opening the browser is the one step CI cannot show.
