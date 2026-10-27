import {
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from "@tanstack/react-router";

import { NAV_ITEMS } from "./nav";
import { Dashboard } from "./pages/Dashboard";
import { DocumentDetailRoute, DocumentsRoute } from "./pages/document-routes";
import { parseDocumentsSearch } from "./pages/documents-search";
import { Import } from "./pages/Import";
import { NotFound } from "./pages/NotFound";
import { Placeholder } from "./pages/Placeholder";
import { Search } from "./pages/Search";
import { Shell } from "./Shell";

/** The route table. Pass a memory history in tests; the browser's history is the default. */
export function createAppRouter(history?: RouterHistory) {
  const root = createRootRoute({ component: Shell, notFoundComponent: NotFound });
  const dashboard = createRoute({
    getParentRoute: () => root,
    path: "/",
    component: Dashboard,
  });
  const documents = createRoute({
    getParentRoute: () => root,
    path: "/documents",
    validateSearch: parseDocumentsSearch,
    component: DocumentsRoute,
  });
  const documentDetail = createRoute({
    getParentRoute: () => root,
    path: "/documents/$documentId",
    component: DocumentDetailRoute,
  });
  const upload = createRoute({
    getParentRoute: () => root,
    path: "/import",
    component: Import,
  });
  // No search parameters: the query is component state only and never part of the address.
  const search = createRoute({
    getParentRoute: () => root,
    path: "/search",
    component: Search,
  });
  const built = new Set(["/", "/documents", "/import", "/search"]);
  const sections = NAV_ITEMS.filter((item) => !built.has(item.path)).map((item) =>
    createRoute({
      getParentRoute: () => root,
      path: item.path,
      component: () => <Placeholder item={item} />,
    }),
  );
  return createRouter({
    routeTree: root.addChildren([
      dashboard,
      documents,
      documentDetail,
      upload,
      search,
      ...sections,
    ]),
    history,
  });
}

export type AppRouter = ReturnType<typeof createAppRouter>;
