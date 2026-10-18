# Frontend

The React app (Vite, TypeScript, Mantine, TanStack Router, as in the stack table of `docs/architecture.md`) that the backend serves in normal use. It has the shell (navigation, light and dark mode, a typed API client that shows the backend's health), the Documents section and the Import page; the other sections are placeholders until their features arrive.

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

`/documents` lists the vault one page (50 documents) at a time, from `GET /api/documents`, with a Library tab and a Trash tab, a sort (date added, name, size) and an order. The backend sorts and pages, so the page never holds more than one page of rows. The sort, order, page and tab are kept in the address (`?sort=name&order=asc&page=2&trash=false`), so a reload or the browser's back button returns to the same view. `src/pages/documents-search.ts` reads them strictly: a value that is not exactly one the list understands falls back to the default instead of being sent to the backend, and a page past the end moves to the last page (a page beyond the backend's largest offset, `MAX_PAGE`, is read as that page first, so no request is out of the backend's range). Each name links to `/documents/<id>`, which shows what `GET /api/documents/{id}` returns: the hash, the version, every import (oldest first, with its original location where the import knew it) and the processing steps.

**Download original** cannot be a plain link: the download needs the token, which travels only in the `Authorization` header (ADR 0016), and a link cannot send a header. `saveOriginal` in `src/api/documents.ts` fetches the bytes with the header, hands them to the browser as an object URL with the document's name in the `download` attribute (the same name the backend gives: the first import's name, or `<id>.<extension>`), and releases the URL afterwards.

**Move to trash** (for a document in use) and **Restore** (for a trashed one) sit next to it on the details page. `moveDocument` in `src/api/documents.ts` calls `POST /api/documents/{id}/trash` or `/restore`, and the page shows the document the backend returns (its new status and the date it went to the trash) with a short confirmation. There is no confirmation dialog, because a restore undoes the move; the button is disabled while its request runs, and a second click in that time sends nothing. Only a 4xx is a definite refusal: the page shows a fixed text chosen by the status code alone (`MOVE_ERROR_TEXT`), never the server's message, and the document stays as it was shown. Every other ending leaves the outcome unknown, because the backend commits the move before it reads the document for its answer. That covers a 5xx, a lost connection, a missing or unreadable answer, and an answer for another document or in a state the move does not give. After an unknown outcome the page loads the document again (`loadDocument`, the same request the page loads with) and shows it as the backend has it, saying "We could not confirm the change". If that load fails too, it says so with a Reload button. Both endpoints are idempotent, so trying the move again is safe. The lists do not have these buttons yet.

**Delete permanently** sits beside **Restore** on a trashed document's page. It first opens a confirmation dialog that names the document and says that the deletion cannot be undone; only its own **Delete permanently** button sends `DELETE /api/documents/{id}` (`deleteDocument` in `src/api/documents.ts`). The same outcome pattern applies. A 204 is done, and the page goes back to the Trash list. A 4xx is a refusal with a fixed text chosen by the status code (`DELETE_ERROR_TEXT`: a 409 means the document is no longer in the trash, a 404 that there is no such document, perhaps deleted already), and the page stays as it was. Anything else (a 5xx, a lost connection, a 2xx other than 204) is unknown: the page loads the document again, goes back to the Trash list if that is a 404 (the delete was made), and otherwise shows the document as it is with "We could not confirm the change", or a Reload button if that load fails too. While a delete or a move runs, both buttons are disabled and a second click sends nothing; a 401 brings the token notice back.

Requests for documents are made only once the session check has accepted the token, so a browser without one makes no request that would be refused. A token can still stop working while the page is open (the server restarted with a new `local-token`): a list, details, download, trash, restore or delete request that is refused (401) asks the session again, so the token notice comes back and pasting the new token reloads the view. A request belongs to the view that asked for it: an answer that arrives after the view changed (another tab, sort or page) is dropped, and the previous view's rows are not shown while the new one loads (`src/api/useDocuments.ts`). Data is fetched with small hooks like the existing health and session checks; the plan's TanStack Query arrives when pages need shared caching and invalidation (trash and restore from the lists). The details page needs neither for trash and restore, and the Import page needs neither for uploads: the details page shows the document the request returns, and the list asks the backend again whenever it is opened, so it shows what was imported.

## Import

`/import` (**Import** in the navigation) uploads files into the vault: drop several files on the drop zone, or pick them with **Choose files** (the chooser offers the extensions the backend's validator accepts, `SUPPORTED_EXTENSIONS`, which a test compares with `EXTENSIONS` in `ingest/validate.py`). The drop zone is a plain Mantine `Paper` with drag events and Mantine's `FileButton`, so no dropzone package is added; on this page a file dropped beside the zone is ignored instead of opened by the browser. `uploadFile` in `src/api/imports.ts` sends each file as the raw body of `POST /api/imports/upload` with its name percent-encoded in `X-Vault-Filename`, its `lastModified` in `X-Vault-Last-Modified` (left out when the backend would refuse it: negative, fractional or past `MAX_MTIME_MS`) and the drop's batch in `X-Vault-Batch`; nothing about the file goes in the address.

The queue (`src/api/useUploads.ts`) sends one file at a time, in the order added, so one page never takes both of the backend's upload slots. The first file of a drop goes without a batch and the rest of that drop with the batch its answer named, so each drop is one import batch; a file whose answer names no batch (a refused request, an unknown outcome) leaves the next file to start it. A drop made during a run joins the end of the queue. The queue is kept above the router, so moving to another page (to open an imported document, say) does not stop it; closing or reloading the tab does.

Each file shows Waiting, Uploading, then one outcome:

- **Imported** or **Already in the vault**, with a link to the document the answer names.
- **Refused**, with a fixed text for the validator's reason (`REFUSAL_TEXT`, one per value of the API's `Reason`, which a test compares with `openapi.json`), for a 413 (too large) or for any other 4xx (the request was refused, most likely for its file name). A folder in a drop and a name that cannot be encoded are refused without sending anything.
- **Not imported** for a 401 or a 503 (no vault): the run stops there, the files still waiting are marked **Not sent**, and a 401 asks the session again so the token notice comes back.
- **Unknown** for a 5xx, a lost connection or a 200 whose body is not a complete answer (a missing document id, an unknown outcome or reason, a batch that is not a ULID): the request may have reached the backend. Adding the file again is safe, because the same bytes become a second source of the same document and show as already in the vault.

Only the status code and the fields of a 200 decide what a row says; the server's own message is never shown. File names are shown in the list and never written to the console or a log (a test runs every outcome with private-looking names and checks the console), and file contents are never read by the page.

## Typed API client

`openapi.json` is the backend's OpenAPI schema, and `src/api/schema.d.ts` is generated from it by `openapi-typescript`. Both are committed, and CI regenerates them and fails if they differ, so the client cannot drift from the backend. After changing an API route or model:

```
cd backend && uv run python -m vault.openapi ../frontend/openapi.json
cd ../frontend && npm run api:generate
```

Calls go through `src/api/client.ts` (`openapi-fetch`), which checks paths and response types at compile time.

## Dependencies

`package-lock.json` is committed and CI installs with `npm ci`. `scripts/check_npm_licenses.py` reads the lock file and fails the build on a disallowed licence or a package whose licence is missing. `npm audit` runs in CI.
