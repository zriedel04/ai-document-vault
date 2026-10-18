import type { Api } from "./client";
import type { components } from "./schema";

export type DocumentItem = components["schemas"]["DocumentItem"];
export type DocumentList = components["schemas"]["DocumentList"];
export type DocumentDetail = components["schemas"]["DocumentDetail"];
export type DocumentState = DocumentItem["state"];
export type DocumentPages = components["schemas"]["DocumentPages"];
export type DocumentPage = components["schemas"]["Page"];

/** Pages the text view asks for at once; the backend allows 1 to 100 (`MAX_WINDOW`). */
export const TEXT_WINDOW = 20;

/** What the text view says when its request fails. A 500 there means the stored text is damaged. */
export const PAGES_ERROR_TEXT: Record<DocumentsError, string> = {
  "needs-token": "This browser has no access to the vault yet. See the notice above.",
  "no-vault": "No vault is open. Start the app with a vault folder.",
  "not-found": "There is no such document.",
  unavailable: "The vault's extracted text of this document is damaged.",
  unreachable: "The text could not be loaded. Check that the backend is running.",
};

export type SortKey = components["schemas"]["SortKey"];
export type Order = components["schemas"]["Order"];
// Every value of each, in one list, so a link or a control can check what it was given.
export const SORT_KEYS = ["added", "name", "size"] as const satisfies readonly SortKey[];
export const ORDERS = ["asc", "desc"] as const satisfies readonly Order[];

/** Documents on one page of the list; the backend allows 1 to 100. */
export const PAGE_SIZE = 50;
/** The largest `offset` the backend's list accepts (`MAX_OFFSET` in `api/documents.py`). */
export const MAX_OFFSET = 1_000_000_000;
/** The last page whose offset the backend accepts. */
export const MAX_PAGE = Math.floor(MAX_OFFSET / PAGE_SIZE) + 1;

/** Why a request for documents gave no answer. */
export type DocumentsError =
  "needs-token" | "no-vault" | "not-found" | "unavailable" | "unreachable";

/** The error a response stands for, by its status code alone (its body is not shown). */
export function errorFor(status: number): DocumentsError {
  if (status === 401) return "needs-token";
  if (status === 503) return "no-vault";
  if (status === 404) return "not-found";
  return "unreachable";
}

export const ERROR_TEXT: Record<DocumentsError, string> = {
  "needs-token": "This browser has no access to the vault yet. See the notice above.",
  "no-vault": "No vault is open. Start the app with a vault folder.",
  "not-found": "There is no such document.",
  unavailable: "The vault's stored copy of this file is missing or damaged.",
  unreachable: "The documents could not be loaded. Check that the backend is running.",
};

export const STATE_LABEL: Record<DocumentState, string> = {
  processing: "Processing",
  searchable: "Searchable",
  ready: "Ready",
  failed: "Failed",
  trashed: "In trash",
};

/**
 * The name a document is saved under: its first import's file name, or `<id>.<extension>` when
 * it has none. The same rule as the backend's download, so the saved file has the same name.
 */
export function fileName(document: Pick<DocumentItem, "id" | "name" | "extension">): string {
  return document.name ?? `${document.id}.${document.extension}`;
}

const UNITS = ["KB", "MB", "GB", "TB"] as const;

/** A size in bytes for people: `512 bytes`, `1.5 KB`, `12 MB` (1 KB = 1024 bytes). */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return bytes === 1 ? "1 byte" : `${String(bytes)} bytes`;
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const shown = value < 10 ? value.toFixed(1).replace(/\.0$/, "") : Math.round(value).toString();
  return `${shown} ${UNITS[unit] ?? "TB"}`;
}

/** A date and time from the API (UTC, ISO 8601) in the browser's own locale and time zone. */
export function formatDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/**
 * Save a document's original file. The download needs the access token, which only travels in
 * the `Authorization` header (ADR 0016), so a plain link cannot fetch it: the page fetches the
 * bytes and hands them to the browser as a file. Returns the error, or null when it was saved.
 */
export async function saveOriginal(
  api: Api,
  document: Pick<DocumentItem, "id" | "name" | "extension">,
): Promise<DocumentsError | null> {
  let blob: Blob | undefined;
  try {
    const { data, response } = await api.GET("/api/documents/{document_id}/file", {
      params: { path: { document_id: document.id } },
      parseAs: "blob",
    });
    // A 500 here is the one the download route gives for a missing or doubled stored copy.
    if (!response.ok) return response.status === 500 ? "unavailable" : errorFor(response.status);
    blob = data;
  } catch {
    return "unreachable";
  }
  if (blob === undefined) return "unreachable";
  const url = URL.createObjectURL(blob);
  const link = window.document.createElement("a");
  link.href = url;
  link.download = fileName(document);
  link.hidden = true;
  window.document.body.append(link);
  link.click();
  link.remove();
  // The browser reads the bytes after the click returns, so the address is released later.
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 60_000);
  return null;
}

/** Moving a document to the trash, or bringing it back. */
export type MoveAction = "trash" | "restore";

/**
 * How a move ended: done (with the document as the backend now has it), refused (a 4xx: the
 * backend did not make the change), or unknown (the request may have reached the backend, but the
 * answer does not prove what it did; the caller must ask for the document again).
 */
export type MoveResult =
  | { status: "ok"; document: DocumentDetail }
  | { status: "error"; error: DocumentsError }
  | { status: "unknown" };

/** The state a document must be in after `action`, if the backend made it. */
function showsMove(document: DocumentDetail, action: MoveAction): boolean {
  return action === "trash" ? document.state === "trashed" : document.state !== "trashed";
}

/**
 * Move a document to the trash or restore it, and return it as the backend now has it. Only the
 * status code decides the error, so nothing the server wrote reaches the page or a log.
 *
 * The backend commits the move before it reads the document for the answer, so a 5xx, a lost
 * connection or an unreadable answer can follow a change that was made: those are "unknown", not
 * failures. Only a 4xx says the change was not made. Both endpoints are idempotent (trashing a
 * trashed document or restoring one in use changes nothing), so trying again is always safe.
 */
export async function moveDocument(
  api: Api,
  documentId: string,
  action: MoveAction,
): Promise<MoveResult> {
  const options = { params: { path: { document_id: documentId } } };
  try {
    const { data, response } =
      action === "trash"
        ? await api.POST("/api/documents/{document_id}/trash", options)
        : await api.POST("/api/documents/{document_id}/restore", options);
    if (response.status >= 400 && response.status < 500) {
      return { status: "error", error: errorFor(response.status) };
    }
    // Anything else proves the change only with the document that was asked for, in the state
    // the move gives: a 5xx, another 2xx or 3xx, a missing body or another document does not.
    if (!response.ok || data?.id !== documentId || !showsMove(data, action)) {
      return { status: "unknown" };
    }
    return { status: "ok", document: data };
  } catch {
    // The request may have been sent before the connection or the answer failed.
    return { status: "unknown" };
  }
}

/** What a refused move (a 4xx) says. The load texts, except for a refusal the page cannot name. */
export const MOVE_ERROR_TEXT: Record<DocumentsError, string> = {
  ...ERROR_TEXT,
  unreachable: "The change was not saved. Reload the page and try again.",
};

/** What the page says when a move's outcome is unknown and the document was loaded again. */
export const MOVE_UNKNOWN_TEXT =
  "We could not confirm the change. Showing the document as it is now.";

/** What it says when the document could not be loaded again either. */
export const MOVE_UNKNOWN_RELOAD_FAILED_TEXT =
  "We could not confirm the change, and the document could not be loaded again, so it may not be shown as it is now.";
