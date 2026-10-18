import type { Api } from "./client";
import type { DocumentsError } from "./documents";
import type { components } from "./schema";

export type Reason = components["schemas"]["Reason"];

/**
 * The extensions the backend's validator accepts (`EXTENSIONS` in `ingest/validate.py`; a test
 * compares the two). Used only to filter the file chooser and to word a refusal: the backend
 * decides.
 */
export const SUPPORTED_EXTENSIONS = ["pdf", "docx", "png", "jpg", "jpeg", "txt", "md"] as const;

/** The latest modification time the backend accepts (`MAX_MTIME_MS` in `api/imports.py`). */
export const MAX_MTIME_MS = 253_402_300_799_999;

/** A batch id as the backend writes it (`ULID` in `db/ids.py`). No "m" flag: the whole value. */
const ULID = /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/;

/**
 * Why a file was not imported, when the backend said so. The validator's reasons, plus a request
 * the backend refused before reading the file (a 400, 413 or other 4xx), and two the page finds
 * itself without sending anything: a name that cannot be encoded, and a folder.
 */
export type Refusal = Reason | "request" | "folder";

/**
 * How one upload ended. Imported or duplicate (with the document the bytes now belong to), refused
 * (nothing was recorded), an error that stops the run (no access, no vault: nothing was recorded),
 * or unknown: a 5xx, a lost connection or an answer that does not prove an outcome. The request may
 * have reached the backend, so the file may have been imported.
 */
export type UploadResult =
  | { status: "imported" | "duplicate"; documentId: string; batchId: string }
  | { status: "refused"; reason: Refusal; batchId: string | null }
  | { status: "error"; error: Extract<DocumentsError, "needs-token" | "no-vault"> }
  | { status: "unknown" };

const types = SUPPORTED_EXTENSIONS.map((extension) => `.${extension}`).join(", ");

/** What each refusal says. Fixed text: nothing the server wrote is shown. */
export const REFUSAL_TEXT: Record<Refusal, string> = {
  not_a_file: "This is not a file the vault can read.",
  unreadable: "The file could not be read.",
  empty: "The file is empty.",
  too_large: "The file is larger than the vault's size limit.",
  unsupported_extension: `This type of file is not supported. Supported types: ${types}.`,
  content_mismatch: "The file's contents do not match its extension.",
  legacy_or_encrypted_office:
    "This is an old-format or password-protected Office file, which the vault cannot read.",
  unreadable_archive: "The Word file could not be opened. It may be damaged.",
  encrypted_archive: "The Word file is encrypted.",
  unsafe_archive: "The Word file is too large inside, has too many parts or has unsafe names.",
  request:
    "The vault did not accept the request. The file name may be too long or contain characters it does not allow.",
  folder: "Folders cannot be uploaded here. Add the files inside it instead.",
};

/** What the page says when an upload's outcome is unknown. */
export const UNKNOWN_TEXT =
  "The upload ended without an answer, so the file may or may not have been imported. Check the documents list, or add the file again: if it was imported, it shows as already in the vault.";

/** The `X-Vault-Last-Modified` value for a `File.lastModified`, or none if the backend would refuse it. */
export function mtimeHeader(lastModified: number): string | undefined {
  return Number.isSafeInteger(lastModified) && lastModified >= 0 && lastModified <= MAX_MTIME_MS
    ? String(lastModified)
    : undefined;
}

/** The name as `X-Vault-Filename` carries it, or null when it cannot be (a lone surrogate). */
function encodeName(name: string): string | null {
  try {
    return encodeURIComponent(name);
  } catch {
    return null;
  }
}

function isReason(value: unknown): value is Reason {
  return (
    typeof value === "string" &&
    value !== "request" &&
    value !== "folder" &&
    Object.hasOwn(REFUSAL_TEXT, value)
  );
}

/** The outcome a 200 proves, or unknown when its body is not a complete answer. */
function readAnswer(data: unknown): UploadResult {
  // Checked field by field: the generated types say what the body should be, not what arrived.
  if (typeof data !== "object" || data === null) return { status: "unknown" };
  const answer = data as Record<string, unknown>;
  const batchId = answer.batch_id;
  if (typeof batchId !== "string" || !ULID.test(batchId)) return { status: "unknown" };
  const documentId = answer.document_id;
  if (answer.outcome === "imported" || answer.outcome === "duplicate") {
    return typeof documentId === "string" && documentId !== ""
      ? { status: answer.outcome, documentId, batchId }
      : { status: "unknown" };
  }
  if (answer.outcome === "refused" && isReason(answer.reason)) {
    return { status: "refused", reason: answer.reason, batchId };
  }
  return { status: "unknown" };
}

/**
 * Upload one file as the request body (`POST /api/imports/upload`), its name, batch and
 * modification time in headers. Never throws. Only the status code and the fields of a 200 decide
 * the result, so nothing the server wrote reaches the page, and nothing here logs: the file name is
 * the owner's own data.
 */
export async function uploadFile(
  api: Api,
  file: File,
  batchId: string | null,
): Promise<UploadResult> {
  const name = encodeName(file.name);
  if (name === null) return { status: "refused", reason: "request", batchId };
  const mtime = mtimeHeader(file.lastModified);
  try {
    const { data, response } = await api.POST("/api/imports/upload", {
      params: {
        header: {
          "x-vault-filename": name,
          ...(batchId === null ? {} : { "x-vault-batch": batchId }),
          ...(mtime === undefined ? {} : { "x-vault-last-modified": mtime }),
        },
      },
      // The schema types a binary body as a string; the browser sends the file's bytes as they are.
      body: file as unknown as string,
      bodySerializer: (body) => body,
      headers: { "Content-Type": "application/octet-stream" },
    });
    if (response.status === 401) return { status: "error", error: "needs-token" };
    // The route answers 503 before it reads the body, when no vault is open.
    if (response.status === 503) return { status: "error", error: "no-vault" };
    if (response.status === 413) return { status: "refused", reason: "too_large", batchId: null };
    // Every other 4xx is a request the backend refused before importing anything.
    if (response.status >= 400 && response.status < 500) {
      return { status: "refused", reason: "request", batchId: null };
    }
    return response.status === 200 ? readAnswer(data) : { status: "unknown" };
  } catch {
    // The request may have been sent before the connection or the answer failed.
    return { status: "unknown" };
  }
}
