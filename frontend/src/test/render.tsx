import { MantineProvider } from "@mantine/core";
import { render } from "@testing-library/react";
import { createMemoryHistory } from "@tanstack/react-router";

import { App } from "../App";
import { createApi } from "../api/client";
import { saveToken } from "../api/token";
import { createAppRouter } from "../router";

export type FetchFn = typeof fetch;

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A token of the shape the backend creates, for tests. */
export const TEST_TOKEN = "t0ken-for-tests_".repeat(3);

/**
 * The backend as the page sees it: `/api/health` is open and `/api/session` needs the bearer
 * token, like the real middleware. Any other path is a 404.
 */
export const healthyBackend: FetchFn = (input) => {
  const request = input as Request;
  const { pathname } = new URL(request.url);
  if (pathname === "/api/health")
    return Promise.resolve(jsonResponse({ status: "ok", version: "1.2.3" }));
  if (pathname === "/api/session") {
    return Promise.resolve(
      request.headers.get("authorization") === `Bearer ${TEST_TOKEN}`
        ? jsonResponse({ id: "local", name: "Local user" })
        : jsonResponse({ detail: "request refused" }, 401),
    );
  }
  return Promise.resolve(jsonResponse({ detail: "not found" }, 404));
};

/**
 * Renders the app once the router has matched the first address, so tests can query at once. The
 * router is returned too, so a test can read the address and the history.
 */
export async function renderApp(options: { route?: string; fetch?: FetchFn; token?: string } = {}) {
  if (options.token) saveToken(options.token);
  const api = createApi(options.fetch ?? healthyBackend);
  const router = createAppRouter(createMemoryHistory({ initialEntries: [options.route ?? "/"] }));
  await router.load();
  const result = render(
    <MantineProvider defaultColorScheme="light">
      <App api={api} router={router} />
    </MantineProvider>,
  );
  return { ...result, router };
}
