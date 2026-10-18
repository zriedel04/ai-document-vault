import { RouterProvider } from "@tanstack/react-router";

import { ApiProvider } from "./api/api-context";
import type { Api } from "./api/client";
import { HealthProvider } from "./api/health-context";
import { SessionProvider } from "./api/session-context";
import { UploadsProvider } from "./api/uploads-context";
import { useHealth } from "./api/useHealth";
import { useSession } from "./api/useSession";
import { useUploads } from "./api/useUploads";
import type { AppRouter } from "./router";

export function App({ api, router }: { api: Api; router: AppRouter }) {
  const health = useHealth(api);
  const session = useSession(api);
  const uploads = useUploads(api, session.recheck);
  return (
    <ApiProvider value={api}>
      <HealthProvider value={health}>
        <SessionProvider value={session}>
          <UploadsProvider value={uploads}>
            <RouterProvider router={router} />
          </UploadsProvider>
        </SessionProvider>
      </HealthProvider>
    </ApiProvider>
  );
}
