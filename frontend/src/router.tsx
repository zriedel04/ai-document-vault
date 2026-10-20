import {
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
} from "@tanstack/react-router";

import { NAV_ITEMS } from "./nav";
import { Dashboard } from "./pages/Dashboard";
import { DocumentDetailRoute, DocumentsRoute } from "./pages/document-routes";
import { parseDocumentSearch, parseDocumentsSearch } from "./pages/documents-search";
import { Import } from "./pages/Import";
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
  const documents = createRoute({
    getParentRoute: () => root,
    path: "/documents",
    validateSearch: parseDocumentsSearch,
    component: DocumentsRoute,
  });
  const documentDetail = createRoute({
    getParentRoute: () => root,
    path: "/documents/$documentId",
    validateSearch: parseDocumentSearch,
    component: DocumentDetailRoute,
  });
  const upload = createRoute({
    getParentRoute: () => root,
    path: "/import",
    component: Import,
  });
  const built = new Set(["/", "/documents", "/import"]);
  const sections = NAV_ITEMS.filter((item) => !built.has(item.path)).map((item) =>
    createRoute({
      getParentRoute: () => root,
      path: item.path,
      component: () => <Placeholder item={item} />,
    }),
  );
  return createRouter({
    routeTree: root.addChildren([dashboard, documents, documentDetail, upload, ...sections]),
    history,
  });
}

export type AppRouter = ReturnType<typeof createAppRouter>;
