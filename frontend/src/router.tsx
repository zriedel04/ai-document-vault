import {
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from "@tanstack/react-router";

import { NAV_ITEMS } from "./nav";
import { Dashboard } from "./pages/Dashboard";
import { NotFound } from "./pages/NotFound";
import { Placeholder } from "./pages/Placeholder";
import { Shell } from "./Shell";

/** The route table. Pass a memory history in tests; the browser's history is the default. */
export function createAppRouter(history?: RouterHistory) {
  const root = createRootRoute({ component: Shell, notFoundComponent: NotFound });
  const dashboard = createRoute({
    getParentRoute: () => root,
    path: "/",
    component: Dashboard,
  });
  const sections = NAV_ITEMS.filter((item) => item.path !== "/").map((item) =>
    createRoute({
      getParentRoute: () => root,
      path: item.path,
      component: () => <Placeholder item={item} />,
    }),
  );
  return createRouter({ routeTree: root.addChildren([dashboard, ...sections]), history });
}

export type AppRouter = ReturnType<typeof createAppRouter>;
