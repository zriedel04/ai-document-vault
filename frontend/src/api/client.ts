import createClient from "openapi-fetch";

import type { paths } from "./schema";

/** A typed client for the backend API. The types come from `openapi.json` (see README). */
export function createApi(fetchImpl?: typeof fetch) {
  // This origin: the backend in normal use, Vite's proxy in development. Request URLs must be
  // absolute, so the origin is spelled out instead of passing an empty base URL.
  return createClient<paths>({
    baseUrl: globalThis.location.origin,
    ...(fetchImpl ? { fetch: fetchImpl } : {}),
  });
}

export type Api = ReturnType<typeof createApi>;
