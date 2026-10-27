import type { Api } from "./client";
import { type DocumentsError, ERROR_TEXT, errorFor } from "./documents";
import type { components } from "./schema";

export type SearchHit = components["schemas"]["SearchHit"];
export type SearchResults = components["schemas"]["SearchResults"];
export type Segment = components["schemas"]["Segment"];
export type SearchType = components["schemas"]["SearchRequest"]["types"][number];

/**
 * Every type the search can filter by, in the order the page offers them. A test compares the
 * list with the enum in `openapi.json`, so a type the backend adds is not left out.
 */
export const SEARCH_TYPES = [
  "pdf",
  "docx",
  "txt",
  "md",
  "png",
  "jpg",
] as const satisfies readonly SearchType[];

export const TYPE_LABEL: Record<SearchType, string> = {
  pdf: "PDF",
  docx: "Word",
  txt: "Text",
  md: "Markdown",
  png: "PNG",
  jpg: "JPEG",
};

/** Documents on one page of results; the backend allows 1 to 50 (`MAX_PAGE` in `keyword.py`). */
export const SEARCH_PAGE_SIZE = 20;
/** The largest `offset` the backend's search accepts (`MAX_OFFSET` in `api/search.py`). */
export const MAX_SEARCH_OFFSET = 1_000_000_000;
/** The last page whose offset the backend accepts. */
export const MAX_SEARCH_PAGE = Math.floor(MAX_SEARCH_OFFSET / SEARCH_PAGE_SIZE) + 1;

/** Why a search gave no results: a documents error, or a query the backend's grammar refuses. */
export type SearchError = DocumentsError | "refused";

export interface SearchQuery {
  query: string;
  types: readonly SearchType[];
  page: number;
}

/**
 * One page of results for `query` (`POST /api/search`). The query travels in the request body,
 * never in the address. Only the status code decides an error, so the server's message (which
 * never quotes the query, but is not the page's to show) is not read: a 422 is a query the
 * backend refuses.
 */
export async function searchDocuments(
  api: Api,
  query: SearchQuery,
): Promise<{ status: "ok"; data: SearchResults } | { status: "error"; error: SearchError }> {
  const { data, response } = await api.POST("/api/search", {
    body: {
      query: query.query,
      limit: SEARCH_PAGE_SIZE,
      offset: (query.page - 1) * SEARCH_PAGE_SIZE,
      types: [...query.types],
    },
  });
  if (data) return { status: "ok", data };
  return {
    status: "error",
    error: response.status === 422 ? "refused" : errorFor(response.status),
  };
}

export const SEARCH_ERROR_TEXT: Record<SearchError, string> = {
  ...ERROR_TEXT,
  "not-found": "The search could not be run. Check that the backend is up to date.",
  unreachable: "The search could not be run. Check that the backend is running.",
  refused:
    "This search cannot be run. A query can have at most 500 characters and 16 words or phrases, and only text characters.",
};
