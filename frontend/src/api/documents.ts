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

/** A document's original bytes, or why they could not be had. */
export type OriginalResult =
  { status: "ok"; blob: Blob } | { status: "error"; error: DocumentsError };

/**
 * Fetch a document's original file through the API client. The download needs the access token,
 * which only travels in the `Authorization` header (ADR 0016), so neither a plain link nor a
 * viewer given an address can fetch it: the page fetches the bytes itself, from an address that
 * holds nothing but the document's id. Only the status code decides the error.
 */
export async function fetchOriginal(api: Api, documentId: string): Promise<OriginalResult> {
  try {
    const { data, response } = await api.GET("/api/documents/{document_id}/file", {
      params: { path: { document_id: documentId } },
      parseAs: "blob",
    });
    // A 500 here is the one the download route gives for a missing or doubled stored copy.
    if (!response.ok) {
      const error = response.status === 500 ? "unavailable" : errorFor(response.status);
      return { status: "error", error };
    }
    if (data === undefined) return { status: "error", error: "unreachable" };
    return { status: "ok", blob: data };
  } catch {
    return { status: "error", error: "unreachable" };
  }
}

/**
 * Save a document's original file: `fetchOriginal` gets the bytes, and the page hands them to the
 * browser as a file. Returns the error, or null when it was saved.
 */
export async function saveOriginal(
  api: Api,
  document: Pick<DocumentItem, "id" | "name" | "extension">,
): Promise<DocumentsError | null> {
  const original = await fetchOriginal(api, document.id);
  if (original.status === "error") return original.error;
  const url = URL.createObjectURL(original.blob);
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

/**
 * Whether the details page shows a document in the PDF viewer: by the media type the import's
 * validator gave it from the file's signature (`ingest/validate.py`), not by its name.
 */
export function isPdf(document: Pick<DocumentItem, "mime">): boolean {
  return document.mime === "application/pdf";
}

/** What the PDF viewer says when the file's bytes could not be fetched. */
export const VIEWER_ERROR_TEXT: Record<DocumentsError, string> = {
  ...ERROR_TEXT,
  unreachable: "The PDF could not be loaded. Check that the backend is running.",
};

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

/** Why a permanent delete was refused: a load error, or a document that is not in the trash. */
export type DeleteError = DocumentsError | "not-in-trash";

/**
 * How a permanent delete ended: deleted (the backend answered 204), refused (a 4xx: nothing was
 * deleted), or unknown (the request may have reached the backend, but the answer does not prove
 * what it did; the caller must ask for the document again, and a 404 then shows it is gone).
 */
export type DeleteResult =
  { status: "deleted" } | { status: "error"; error: DeleteError } | { status: "unknown" };

/**
 * Delete a document in the trash for good (`DELETE /api/documents/{id}`). Only the status code
 * decides, so nothing the server wrote reaches the page. A 409 is the backend's refusal of a
 * document that is not in the trash. A 404 is a refusal too, also when an earlier attempt whose
 * answer was lost had deleted it: the page cannot tell the two apart, and its text says so.
 */
export async function deleteDocument(api: Api, documentId: string): Promise<DeleteResult> {
  try {
    const { response } = await api.DELETE("/api/documents/{document_id}", {
      params: { path: { document_id: documentId } },
    });
    if (response.status === 409) return { status: "error", error: "not-in-trash" };
    if (response.status >= 400 && response.status < 500) {
      return { status: "error", error: errorFor(response.status) };
    }
    // Only the backend's own answer for a delete proves it: 204. A 5xx, another 2xx or a 3xx
    // does not.
    return response.status === 204 ? { status: "deleted" } : { status: "unknown" };
  } catch {
    // The request may have been sent before the connection or the answer failed.
    return { status: "unknown" };
  }
}

/** What a refused delete says. */
export const DELETE_ERROR_TEXT: Record<DeleteError, string> = {
  ...ERROR_TEXT,
  "not-found": "There is no such document. It may have been deleted already.",
  "not-in-trash":
    "Only a document in the trash can be deleted permanently. Reload the page to see where it is.",
  unreachable: "Nothing was deleted. Reload the page and try again.",
};

/** Why a retry was refused: a load error, or a document in the trash. */
export type RetryError = DocumentsError | "in-trash";

/**
 * How a retry ended: done (with the document as the backend now has it), refused (a 4xx: nothing
 * was queued), or unknown (the request may have reached the backend; ask for the document again).
 */
export type RetryResult =
  | { status: "ok"; document: DocumentDetail }
  | { status: "error"; error: RetryError }
  | { status: "unknown" };

/**
 * The jobs a Retry queues again, as the backend marks them (`retryable`): a failed job that is its
 * document's current work for its step, decided by the queue's own rule from the document as it is
 * now. The order of the jobs alone cannot show it: a document that moved to other bytes and back
 * makes its older job current again.
 */
export function retryableJobs(document: DocumentDetail): DocumentDetail["jobs"] {
  return document.jobs.filter((job) => job.retryable);
}

/**
 * Queue a document's failed processing again (`POST /api/documents/{id}/retry`) and return it as
 * the backend now has it. Only the status code decides the error. The backend queues before it
 * reads the document for the answer, so a 5xx, a lost connection or an answer for another
 * document is "unknown". Pressing again is safe: a retry queues the same work once.
 */
export async function retryDocument(api: Api, documentId: string): Promise<RetryResult> {
  try {
    const { data, response } = await api.POST("/api/documents/{document_id}/retry", {
      params: { path: { document_id: documentId } },
    });
    if (response.status === 409) return { status: "error", error: "in-trash" };
    if (response.status >= 400 && response.status < 500) {
      return { status: "error", error: errorFor(response.status) };
    }
    if (!response.ok || data?.id !== documentId) return { status: "unknown" };
    return { status: "ok", document: data };
  } catch {
    // The request may have been sent before the connection or the answer failed.
    return { status: "unknown" };
  }
}

/** What a refused retry says. */
export const RETRY_ERROR_TEXT: Record<RetryError, string> = {
  ...ERROR_TEXT,
  "in-trash": "A document in the trash is not processed. Restore it first.",
  unreachable: "Nothing was queued. Reload the page and try again.",
};
