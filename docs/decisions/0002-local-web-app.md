# ADR 0002: Local web app instead of a native desktop app

- Status: Accepted
- Date: 2026-10-01
- Hardware-dependent: no
- Plan reference: docs/architecture.md, sections 2 and 22 (decision 1)

## Context

The app runs on the owner's Windows desktop. Secure browser access from another computer (a Mac) is a later goal, not part of the MVP. The UI needs a document viewer, tables, forms and streamed answers.

## Decision

One Python (FastAPI) process owns the API, the background worker, the database and the vault folder. It binds to `127.0.0.1:8765` and serves the built React UI to the user's browser. In development, Vite's dev server proxies `/api` to the backend.

## Alternatives considered

- **Native desktop app (Tauri or Electron)**: a native window, but remote browser access would need a second code path. A Tauri wrapper can still be added later around the same UI.
- **Python GUI toolkit (for example Qt)**: no browser dependency, but a weaker document-viewer and component ecosystem and no path to remote access.

## Consequences

- The same UI works locally now and over a secure tunnel later.
- Any website open in the browser can send requests to localhost. The server must reject foreign `Host` and `Origin` values and require a per-install token (plan section 10, built in the backend security PR).
- No tray icon or run-at-logon until a later phase.
