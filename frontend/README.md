# Frontend

The React app (Vite, TypeScript, Mantine) that the backend serves in normal use. This is the shell: navigation, light and dark mode, and a typed API client that shows the backend's health. Each section is a placeholder until its feature arrives.

## Commands

Run from this folder. Node 22 or newer is required.

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

## Typed API client

`openapi.json` is the backend's OpenAPI schema, and `src/api/schema.d.ts` is generated from it by `openapi-typescript`. Both are committed, and CI regenerates them and fails if they differ, so the client cannot drift from the backend. After changing an API route or model:

```
cd backend && uv run python -m vault.openapi ../frontend/openapi.json
cd ../frontend && npm run api:generate
```

Calls go through `src/api/client.ts` (`openapi-fetch`), which checks paths and response types at compile time.

## Dependencies

`package-lock.json` is committed and CI installs with `npm ci`. `scripts/check_npm_licenses.py` reads the lock file and fails the build on a disallowed licence or a package whose licence is missing. `npm audit` runs in CI.
