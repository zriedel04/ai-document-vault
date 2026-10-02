import { RouterProvider } from "@tanstack/react-router";

import type { Api } from "./api/client";
import { HealthProvider } from "./api/health-context";
import { useHealth } from "./api/useHealth";
import type { AppRouter } from "./router";

export function App({ api, router }: { api: Api; router: AppRouter }) {
  const health = useHealth(api);
  return (
    <HealthProvider value={health}>
      <RouterProvider router={router} />
    </HealthProvider>
  );
}
