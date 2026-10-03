import { RouterProvider } from "@tanstack/react-router";

import type { Api } from "./api/client";
import { HealthProvider } from "./api/health-context";
import { SessionProvider } from "./api/session-context";
import { useHealth } from "./api/useHealth";
import { useSession } from "./api/useSession";
import type { AppRouter } from "./router";

export function App({ api, router }: { api: Api; router: AppRouter }) {
  const health = useHealth(api);
  const session = useSession(api);
  return (
    <HealthProvider value={health}>
      <SessionProvider value={session}>
        <RouterProvider router={router} />
      </SessionProvider>
    </HealthProvider>
  );
}
