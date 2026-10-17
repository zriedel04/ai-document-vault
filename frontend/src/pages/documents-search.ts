import { MAX_PAGE, ORDERS, type Order, SORT_KEYS, type SortKey } from "../api/documents";

/** What the document list shows, kept in the address so a page can be bookmarked or shared. */
export interface DocumentsSearch {
  sort: SortKey;
  order: Order;
  page: number;
  trash: boolean;
}

export const DEFAULT_SEARCH: DocumentsSearch = {
  sort: "added",
  order: "desc",
  page: 1,
  trash: false,
};

function oneOf<T extends string>(values: readonly T[], value: unknown, fallback: T): T {
  return values.find((item) => item === value) ?? fallback;
}

/**
 * Read the address's search part. Each value must be exactly one the list understands (a page is
 * a whole number from 1); anything else, typed by hand or left over from an old link, falls back
 * to the default instead of reaching the backend. A page beyond the last one the backend can
 * reach (`MAX_PAGE`) becomes that page, and the list then moves to its real last page.
 */
export function parseDocumentsSearch(search: Record<string, unknown>): DocumentsSearch {
  const page = search.page;
  return {
    sort: oneOf(SORT_KEYS, search.sort, DEFAULT_SEARCH.sort),
    order: oneOf(ORDERS, search.order, DEFAULT_SEARCH.order),
    page:
      typeof page === "number" && Number.isSafeInteger(page) && page >= 1
        ? Math.min(page, MAX_PAGE)
        : DEFAULT_SEARCH.page,
    trash: search.trash === true,
  };
}
