# ADR 0016: Local API token: bearer header only, handed to the page in the address fragment

- Status: Accepted
- Date: 2026-10-03
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 10 (application security)

## Context

Binding the server to `127.0.0.1` and checking `Host` and `Origin` keeps other computers and other websites out. It does not keep out another program on the same PC, or another Windows account, that simply calls `http://127.0.0.1:<port>/api/...`. The plan therefore asks for a random per-install token, and left open whether it travels in a `SameSite=Strict` cookie or in a header.

The token has two jobs that pull against each other: the page must hold it, and nobody else on the PC may be able to get it by asking the server.

## Decision

- The token is the file `local-token` in the vault folder (created once, owner-only, backed up with the vault). Deleting it and restarting issues a new one.
- The server accepts it only as `Authorization: Bearer <token>`. No cookie is set or read. Only `GET /api/health` is open.
- **The server never hands the token out.** `index.html` and every other file are static, and there is no endpoint that returns the token.
- The launcher reads the file and opens the browser at `http://127.0.0.1:<port>/#token=<token>`. A fragment is never sent to a server, so it appears in no request, no access log and no `Referer`. The page (`frontend/src/api/token.ts`) takes it before anything else runs, keeps it in `sessionStorage` and removes the fragment from the address bar and history entry, whether or not the token was usable.
- Every API call from the page sends the stored token as the bearer header, to the page's own origin only.
- A page with no usable token makes no API call that needs one and shows a notice with a password field. The owner can paste the token from `local-token` there. A token the server refuses (for example after a new one was issued) shows the same notice.

## Alternatives considered

- **`SameSite=Strict` cookie**: tried first and dropped. Cookies are scoped by host and not by port, so another local program serving a page on `localhost:3000` would have received the token, and `SameSite` does not separate ports.
- **The server puts the token into `index.html` or returns it from an endpoint**: any local process can request the page, so the token would protect nothing from the callers it is meant to stop. The `Host` check does not help against a program that sends `Host: 127.0.0.1`.
- **`?token=` in the query string**: the query reaches the server, the access log (the server's own logger writes request URLs) and `Referer`. A fragment does not.
- **`localStorage`**: survives closing the browser and so makes a bookmark work, but it keeps the token on disk in the browser profile and for every later page load of the origin. `sessionStorage` ends with the tab. The price is that a new tab or a bookmark needs the launcher or a paste.
- **No token, rely on `Host` and `Origin` only**: leaves the PC's other programs and accounts with full access to the vault.
- **A single-use code in the address that the page exchanges for the token**: closes the process-list exposure below. Not done now because it adds an endpoint and state to a design the owner has not asked to be that strict; it is the follow-up if the PC is shared with other accounts.

## Consequences

- Reloading the page in the same tab keeps working. A new tab, a bookmark or a restarted browser needs the launcher or a paste.
- Any script that runs in the page's origin can read the token from `sessionStorage`. That makes injected HTML the main risk: the app must keep rendering document text as text (React does this by default), and a `Content-Security-Policy` response header should be added when the first page that shows document content is built.
- While the browser starts, the token is in its command line. Windows normally lets only an administrator read another account's process command lines, so this matters only if an untrusted account has administrator rights. The single-use code above is the answer if that ever changes.
- During development the page runs on Vite's port, which is a different origin: open `http://localhost:5173/#token=<token>` once, or paste the token.
- The launcher (`vault.ps1`) has to read `local-token` and build that address; until it exists, the owner pastes the token.
- If a later feature needs real login (remote access), `current_user` and this token are replaced together by a new ADR.
