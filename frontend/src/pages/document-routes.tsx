import { useNavigate, useParams, useSearch } from "@tanstack/react-router";

import { DocumentDetail } from "./DocumentDetail";
import { Documents } from "./Documents";
import { type DocumentsSearch, parseDocumentsSearch } from "./documents-search";

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
  const id = params.documentId;
  return <DocumentDetail documentId={typeof id === "string" ? id : ""} />;
}
