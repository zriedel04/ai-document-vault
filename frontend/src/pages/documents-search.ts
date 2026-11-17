import {
  DOCUMENT_TYPES,
  type DocumentType,
  FILTER_STATES,
  type FilterState,
  MAX_PAGE,
  ORDERS,
  type Order,
  SORT_KEYS,
  type SortKey,
} from "../api/documents";
import { isLabelId } from "../api/labels";
import type { SearchHit } from "../api/search";

/** What the document list shows, kept in the address so a page can be bookmarked or shared. */
export interface DocumentsSearch {
  sort: SortKey;
  order: Order;
  page: number;
  trash: boolean;
  /**
   * The label whose documents (its own and those of every label below it) the list shows, by its
   * id; left out for every document, so the address holds no `label` then. Only the id goes in
   * the address, never a label's name.
   */
  label?: string;
  /** Only documents of these types, in `DOCUMENT_TYPES` order; left out for every type. */
  type?: DocumentType[];
  /**
   * Only documents in these states, in `FILTER_STATES` order (in the trash, the state each had
   * when it went there); left out for every state.
   */
  state?: FilterState[];
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
 * The values of a list in the address that are all among `values`: each one once, in the order of
 * `values`. Anything else (not a list, a value the list does not know, a list of nothing) gives
 * none, so the filter falls back to every value; no token outside `values` is ever kept.
 */
function someOf<T extends string>(values: readonly T[], value: unknown): T[] {
  if (!Array.isArray(value) || value.length === 0) return [];
  const given: unknown[] = value;
  if (!given.every((item) => values.some((known) => known === item))) return [];
  return values.filter((known) => given.includes(known));
}

/** The type and state filters as the address holds them: left out when they keep every value. */
function filters(
  type: DocumentType[],
  state: FilterState[],
): Pick<DocumentsSearch, "type" | "state"> {
  return { ...(type.length ? { type } : {}), ...(state.length ? { state } : {}) };
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
    ...(isLabelId(search.label) ? { label: search.label } : {}),
    ...filters(someOf(DOCUMENT_TYPES, search.type), someOf(FILTER_STATES, search.state)),
  };
}

/** The same list, with its other filters, filtered by another label or by none (`null`). */
export function withLabel(search: DocumentsSearch, label: string | null): DocumentsSearch {
  const rest = {
    sort: search.sort,
    order: search.order,
    trash: search.trash,
    page: 1,
    ...filters(search.type ?? [], search.state ?? []),
  };
  return label === null ? rest : { ...rest, label };
}

/**
 * The same list, with its label, filtered by other types and states (each in its list's order,
 * each once; none for every value), from its first page.
 */
export function withFilters(
  search: DocumentsSearch,
  type: readonly string[],
  state: readonly string[],
): DocumentsSearch {
  const rest = {
    sort: search.sort,
    order: search.order,
    trash: search.trash,
    page: 1,
    ...(search.label === undefined ? {} : { label: search.label }),
  };
  return {
    ...rest,
    ...filters(
      DOCUMENT_TYPES.filter((known) => type.includes(known)),
      FILTER_STATES.filter((known) => state.includes(known)),
    ),
  };
}

/**
 * What a document's page keeps in the address: the PDF page to open at (`?page=4`), so a link
 * such as a citation can open the viewer there. A page number is not private, unlike a search
 * query. Anything but a whole number from 1 is left out and the viewer opens at the first page; a
 * page past the end of the PDF is known only once it has loaded, and the viewer handles it there.
 */
export interface DocumentSearch {
  page?: number;
}

export function parseDocumentSearch(search: Record<string, unknown>): DocumentSearch {
  const page = search.page;
  return typeof page === "number" && Number.isSafeInteger(page) && page >= 1 ? { page } : {};
}

/**
 * Where a result opens its document: a PDF at the result's page, read by the same parser as the
 * address of the document's page (`parseDocumentSearch`), so a page the viewer would refuse is left
 * out and the viewer opens at its first page; a page past the end is the viewer's to handle once
 * the PDF has loaded. Every other type opens at its start: DOCX, TXT and Markdown are stored as one
 * page, which the text view's first window always holds whole. Only the page number goes in the
 * address, never the query or anything else of the result.
 */
export function resultPlace(hit: Pick<SearchHit, "extension" | "page">): DocumentSearch {
  return hit.extension === "pdf" ? parseDocumentSearch({ page: hit.page }) : {};
}
