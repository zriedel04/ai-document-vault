import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import { DocumentDetail } from "./DocumentDetail";
import { Documents } from "./Documents";
import {
  type DocumentsSearch,
  parseDocumentSearch,
  parseDocumentsSearch,
} from "./documents-search";

// The router's types are not registered globally, so these read the address loosely and check it
// again here: the search through the same parser the route validates with.

export function DocumentsRoute() {
  const search = parseDocumentsSearch(useSearch({ strict: false }));
  const navigate = useNavigate();
  return (
    <Documents
      search={search}
      onSearch={(next: DocumentsSearch) => void navigate({ to: "/documents", search: next })}
    />
  );
}

export function DocumentDetailRoute() {
  const params: Record<string, unknown> = useParams({ strict: false });
  const raw = params.documentId;
  const id = typeof raw === "string" ? raw : "";
  const { page } = parseDocumentSearch(useSearch({ strict: false }));
  const navigate = useNavigate();
  return (
    <DocumentDetail
      documentId={id}
      page={page}
      onPageChange={(next: number) =>
        // A new history entry per page, so Back returns to the page before; the details page
        // stays where it was scrolled instead of jumping to the top.
        void navigate({
          to: "/documents/$documentId",
          params: { documentId: id },
          search: { page: next },
          resetScroll: false,
        })
      }
    />
  );
}
