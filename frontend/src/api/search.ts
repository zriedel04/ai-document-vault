import type { Api } from "./client";
import { type DocumentsError, ERROR_TEXT, errorFor } from "./documents";
import { NO_LABEL_TEXT } from "./labels";
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

/**
 * Why a search gave no results: a documents error, a query the backend's grammar refuses, or a
 * label filter on a label the vault no longer has.
 */
export type SearchError = DocumentsError | "refused" | "no-label";

export interface SearchQuery {
  query: string;
  types: readonly SearchType[];
  /** A label's id: only the documents that carry it or a label below it; null for every one. */
  label: string | null;
  /** The import bounds as instants (`importedRange`), each null for an open side. */
  importedFrom: string | null;
  importedTo: string | null;
  page: number;
}

/**
 * The days the import date filter offers. The backend takes any instant from the year 1 to 9999
 * in UTC; the page stays within 1970 (nothing is imported before it) and the day before the
 * last, so the end of a day in any zone is still an instant the backend accepts.
 */
export const FIRST_IMPORT_DAY = "1970-01-01";
export const LAST_IMPORT_DAY = "9999-12-30";

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * Local midnight starting the day `date` of `month` (from 1) of `year` in the browser's time
 * zone; a date past the month's end rolls into the next. Where the clocks change at midnight the
 * day starts when it does (01:00). `setFullYear`, not the constructor, which reads the years 0 to
 * 99 as 1900 to 1999.
 */
function midnight(year: number, month: number, date: number): Date {
  const start = new Date(0);
  start.setFullYear(year, month - 1, date);
  start.setHours(0, 0, 0, 0);
  return start;
}

/**
 * `day` ("YYYY-MM-DD", as a date field gives it) as year, month and date, or null for a day that
 * does not exist or is outside the days offered.
 */
function calendarDay(day: string): [number, number, number] | null {
  const parts = DAY.exec(day);
  if (parts === null || day < FIRST_IMPORT_DAY || day > LAST_IMPORT_DAY) return null;
  const [year, month, date] = [Number(parts[1]), Number(parts[2]), Number(parts[3])];
  const start = midnight(year, month, date);
  if (start.getFullYear() !== year || start.getMonth() !== month - 1 || start.getDate() !== date)
    return null;
  return [year, month, date];
}

/** The first instant of `day` in the browser's time zone, in UTC, or null (see `calendarDay`). */
export function dayStart(day: string): string | null {
  const parts = calendarDay(day);
  return parts === null ? null : midnight(...parts).toISOString();
}

/**
 * The last instant of `day` in the browser's time zone, in UTC to the microsecond the backend
 * stores: one microsecond before the next day starts. A Date counts milliseconds, so it is the
 * millisecond before, written with three more nines.
 */
export function dayEnd(day: string): string | null {
  const parts = calendarDay(day);
  if (parts === null) return null;
  const [year, month, date] = parts;
  const next = midnight(year, month, date + 1);
  return new Date(next.getTime() - 1).toISOString().replace(/Z$/, "999Z");
}

/** Why the chosen days cannot be searched. */
export type ImportRangeError = "reversed" | "invalid";

export const RANGE_ERROR_TEXT: Record<ImportRangeError, string> = {
  reversed: "The first day is after the last day. Choose a first day on or before the last.",
  invalid: `Choose days between ${FIRST_IMPORT_DAY} and ${LAST_IMPORT_DAY}.`,
};

/**
 * The import bounds for the days the owner picked ("" for none): from the first instant of the
 * first day to the last instant of the last, both in the browser's time zone, because a day is
 * the owner's day and the backend, which stores UTC and has no zone database on Windows, takes
 * instants with their offset. Either side may be open.
 */
export function importedRange(
  first: string,
  last: string,
): { from: string | null; to: string | null } | { error: ImportRangeError } {
  const from = first === "" ? null : dayStart(first);
  const to = last === "" ? null : dayEnd(last);
  if ((first !== "" && from === null) || (last !== "" && to === null)) return { error: "invalid" };
  if (first !== "" && last !== "" && first > last) return { error: "reversed" };
  return { from, to };
}

/**
 * One page of results for `query` (`POST /api/search`). The query travels in the request body,
 * never in the address. Only the status code decides an error, so the server's message (which
 * never quotes the query, but is not the page's to show) is not read: a 422 is a query the
 * backend refuses (the page sends only import bounds `importedRange` made, which it accepts), and a 404 with a label is the backend's answer for a label it does not have.
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
      ...(query.label === null ? {} : { label: query.label }),
      ...(query.importedFrom === null ? {} : { imported_from: query.importedFrom }),
      ...(query.importedTo === null ? {} : { imported_to: query.importedTo }),
    },
  });
  if (data) return { status: "ok", data };
  if (response.status === 404 && query.label !== null)
    return { status: "error", error: "no-label" };
  return {
    status: "error",
    error: response.status === 422 ? "refused" : errorFor(response.status),
  };
}

export const SEARCH_ERROR_TEXT: Record<SearchError, string> = {
  ...ERROR_TEXT,
  "not-found": "The search could not be run. Check that the backend is up to date.",
  unreachable: "The search could not be run. Check that the backend is running.",
  "no-label": NO_LABEL_TEXT,
  refused:
    "This search cannot be run. A query can have at most 500 characters and 16 words or phrases, and only text characters.",
};
