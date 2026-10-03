import createClient from "openapi-fetch";

import type { paths } from "./schema";
import { readToken } from "./token";

/**
 * A typed client for the backend API. The types come from `openapi.json` (see README).
 *
 * Every request carries the access token as `Authorization: Bearer`, when there is one. The base
 * URL is this page's own origin, so the token is never sent anywhere else. `getToken` is a
 * parameter so a test can supply one.
 */
export function createApi(fetchImpl?: typeof fetch, getToken: () => string | null = readToken) {
  // This origin: the backend in normal use, Vite's proxy in development. Request URLs must be
  // absolute, so the origin is spelled out instead of passing an empty base URL.
  const client = createClient<paths>({
    baseUrl: globalThis.location.origin,
    ...(fetchImpl ? { fetch: fetchImpl } : {}),
  });
  client.use({
    onRequest({ request }) {
      const token = getToken();
      if (token) request.headers.set("Authorization", `Bearer ${token}`);
      return request;
    },
  });
  return client;
}

export type Api = ReturnType<typeof createApi>;
