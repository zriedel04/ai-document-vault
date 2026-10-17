# Frontend

The React app (Vite, TypeScript, Mantine, TanStack Router, as in the stack table of `docs/architecture.md`) that the backend serves in normal use. It has the shell (navigation, light and dark mode, a typed API client that shows the backend's health) and the Documents section; the other sections are placeholders until their features arrive.

## Commands

Run from this folder. Node 22.13 or newer on an even-numbered line is required (22.13+, 24 or 26+). The range in `package.json` is the intersection of the `engines` of the locked packages (Vite needs 22.12+, ESLint and jsdom 22.13+, Vitest only even-numbered releases); re-derive it when a dependency update changes one.

| Task                                                         | Command                                   |
| ------------------------------------------------------------ | ----------------------------------------- |
| Install                                                      | `npm ci`                                  |
| Dev server (proxies `/api` to the backend on 127.0.0.1:8765) | `npm run dev`                             |
| Tests                                                        | `npm test`                                |
| Lint and format check                                        | `npm run lint` and `npm run format:check` |
| Type check and build into `dist/`                            | `npm run build`                           |
| Regenerate the API types                                     | `npm run api:generate`                    |
| Licence check (from the repository root)                     | `python scripts/check_npm_licenses.py`    |

## One origin

In normal use the backend serves `dist/` and the API from the same address, so there is no CORS setup and the app calls `/api/...` on its own origin. During development, Vite serves the app and proxies `/api` to the backend, which gives the browser the same single origin. Start both: `uv run python -m vault` in `backend/` and `npm run dev` here.

## Access token

Every `/api` call except `/api/health` needs the install's token as `Authorization: Bearer` (ADR 0016). The launcher opens the app at `http://127.0.0.1:8765/#token=<token>`. `src/api/token.ts` takes the token from that fragment before anything else runs, stores it in `sessionStorage` (this tab only; another local port cannot read it) and removes the fragment from the address bar. The API client (`src/api/client.ts`) then adds the header to each request, to the page's own origin only. With no stored token the page asks nothing of the API that needs one and shows a notice with a password field, where the token from the file `local-token` in the vault folder can be pasted. In development, open `http://localhost:5173/#token=<token>` once or paste the token. A token the server refuses shows the same notice.

## Documents

`/documents` lists the vault one page (50 documents) at a time, from `GET /api/documents`, with a Library tab and a Trash tab, a sort (date added, name, size) and an order. The backend sorts and pages, so the page never holds more than one page of rows. The sort, order, page and tab are kept in the address (`?sort=name&order=asc&page=2&trash=false`), so a reload or the browser's back button returns to the same view. `src/pages/documents-search.ts` reads them strictly: a value that is not exactly one the list understands falls back to the default instead of being sent to the backend, and a page past the end moves to the last page. Each name links to `/documents/<id>`, which shows what `GET /api/documents/{id}` returns: the hash, the version, every import (oldest first, with its original location where the import knew it) and the processing steps.

**Download original** cannot be a plain link: the download needs the token, which travels only in the `Authorization` header (ADR 0016), and a link cannot send a header. `saveOriginal` in `src/api/documents.ts` fetches the bytes with the header, hands them to the browser as an object URL with the document's name in the `download` attribute (the same name the backend gives: the first import's name, or `<id>.<extension>`), and releases the URL afterwards.

Requests for documents are made only once the session check has accepted the token, so a browser without one makes no request that would be refused. A request belongs to the view that asked for it: an answer that arrives after the view changed (another tab, sort or page) is dropped, and the previous view's rows are not shown while the new one loads (`src/api/useDocuments.ts`). Data is fetched with small hooks like the existing health and session checks; the plan's TanStack Query arrives when pages need shared caching and invalidation (upload, trash and restore).

## Typed API client

`openapi.json` is the backend's OpenAPI schema, and `src/api/schema.d.ts` is generated from it by `openapi-typescript`. Both are committed, and CI regenerates them and fails if they differ, so the client cannot drift from the backend. After changing an API route or model:

```
cd backend && uv run python -m vault.openapi ../frontend/openapi.json
cd ../frontend && npm run api:generate
```

Calls go through `src/api/client.ts` (`openapi-fetch`), which checks paths and response types at compile time.

## Dependencies

`package-lock.json` is committed and CI installs with `npm ci`. `scripts/check_npm_licenses.py` reads the lock file and fails the build on a disallowed licence or a package whose licence is missing. `npm audit` runs in CI.
