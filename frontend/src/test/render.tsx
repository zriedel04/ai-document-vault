import { MantineProvider } from "@mantine/core";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { App } from "../App";
import { createApi } from "../api/client";

export type FetchFn = typeof fetch;

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export const healthyBackend: FetchFn = () =>
  Promise.resolve(jsonResponse({ status: "ok", version: "1.2.3" }));

export function renderApp(options: { route?: string; fetch?: FetchFn } = {}) {
  const api = createApi(options.fetch ?? healthyBackend);
  return render(
    <MantineProvider defaultColorScheme="light">
      <MemoryRouter initialEntries={[options.route ?? "/"]}>
        <App api={api} />
      </MemoryRouter>
    </MantineProvider>,
  );
}
