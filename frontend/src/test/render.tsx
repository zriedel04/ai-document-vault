import { MantineProvider } from "@mantine/core";
import { render } from "@testing-library/react";
import { createMemoryHistory } from "@tanstack/react-router";

import { App } from "../App";
import { createApi } from "../api/client";
import { createAppRouter } from "../router";

export type FetchFn = typeof fetch;

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export const healthyBackend: FetchFn = () =>
  Promise.resolve(jsonResponse({ status: "ok", version: "1.2.3" }));

/** Renders the app once the router has matched the first address, so tests can query at once. */
export async function renderApp(options: { route?: string; fetch?: FetchFn } = {}) {
  const api = createApi(options.fetch ?? healthyBackend);
  const router = createAppRouter(createMemoryHistory({ initialEntries: [options.route ?? "/"] }));
  await router.load();
  return render(
    <MantineProvider defaultColorScheme="light">
      <App api={api} router={router} />
    </MantineProvider>,
  );
}
