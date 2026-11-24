import { useApi } from "./api-context";
import type { Api } from "./client";
import { type DocumentDetail, type DocumentsError, ERROR_TEXT, errorFor } from "./documents";
import type { components } from "./schema";
import { type Request, useRequest } from "./useDocuments";

export type LabelNode = components["schemas"]["LabelNode"];
export type LabelTree = components["schemas"]["LabelTree"];

/**
 * A label id as the backend writes it (a ULID: 26 characters of Crockford's base32, the first 0
 * to 7). The document list keeps the chosen label's id in the address, so an id from an old link
 * or typed by hand is checked against this shape before it reaches the backend.
 */
const LABEL_ID = /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/;

export function isLabelId(value: unknown): value is string {
  return typeof value === "string" && LABEL_ID.test(value);
}

/** Between the levels of a label's path, as the search results show a section path. */
export const PATH_SEPARATOR = " › ";

/**
 * What the filter shows for a label: its whole path, so two labels of the same name in different
 * places can be told apart, and, when asked, how many documents in use it holds (itself and every
 * label below it). The names are the owner's text and are only ever rendered as text.
 */
export function labelText(label: Pick<LabelNode, "path" | "documents">, counts: boolean): string {
  const path = label.path.join(PATH_SEPARATOR);
  return counts ? `${path} (${String(label.documents)})` : path;
}

/**
 * Every label as a tree, in the backend's order (each parent before its children), for the label
 * filters. Asked for once per page that shows a filter; `reload` asks again.
 */
export function useLabels(): Request<LabelTree> {
  const api = useApi();
  return useRequest("labels", async () => {
    const { data, response } = await api.GET("/api/labels");
    return data ? { status: "ok", data } : { status: "error", error: errorFor(response.status) };
  });
}

/** What a filter on a label the vault no longer has says (the backend's 404 for the label). */
export const NO_LABEL_TEXT =
  "That label is no longer in the vault. Choose another label, or show every document.";

/** The kinds of label the owner makes (`OwnerKind` in `api/labels.py`); folders are the imports'. */
export type OwnerKind = components["schemas"]["NewLabel"]["kind"];
export const OWNER_KINDS = ["collection", "tag"] as const satisfies readonly OwnerKind[];

export function isOwnerKind(kind: string): kind is OwnerKind {
  return (OWNER_KINDS as readonly string[]).includes(kind);
}

/** The owner's own assignments on a document: each collection or tag they gave it. */
export function ownerAssignments(document: DocumentDetail): DocumentDetail["labels"] {
  return document.labels.filter(
    (label) => isOwnerKind(label.kind) && label.source === "user" && label.status === "accepted",
  );
}

/** Why a new label was not made: a load error, a name with nothing left, or a name taken. */
export type CreateError = DocumentsError | "bad-name" | "name-taken";

/**
 * How making a label ended: made (with the label as the tree shows it), refused (a 4xx: nothing
 * was made), or unknown (the request may have reached the backend; ask for the labels again).
 */
export type CreateResult =
  | { status: "ok"; label: LabelNode }
  | { status: "error"; error: CreateError }
  | { status: "unknown" };

/**
 * Make a collection or a tag at the top of its tree (`POST /api/labels`). Only the status code
 * decides the error, so nothing the server wrote reaches the page: a 422 is a name the backend's
 * cleaning leaves empty or too long, and a 409 at the top of the tree is a name already there.
 */
export async function createLabel(api: Api, kind: OwnerKind, name: string): Promise<CreateResult> {
  try {
    const { data, response } = await api.POST("/api/labels", { body: { kind, name } });
    if (response.status === 422) return { status: "error", error: "bad-name" };
    if (response.status === 409) return { status: "error", error: "name-taken" };
    if (response.status >= 400 && response.status < 500) {
      return { status: "error", error: errorFor(response.status) };
    }
    if (response.status !== 201 || data?.kind !== kind) return { status: "unknown" };
    return { status: "ok", label: data };
  } catch {
    return { status: "unknown" };
  }
}

/** Why an assignment was not changed: a load error, or a label the owner does not manage. */
export type AssignError = DocumentsError | "not-owners";

export type AssignResult =
  | { status: "ok"; document: DocumentDetail }
  | { status: "error"; error: AssignError }
  | { status: "unknown" };

/**
 * Put a document in a collection or give it a tag (`assigned` true), or take it off, and return
 * the document as the backend now has it. Both are idempotent, so trying again is always safe. A
 * 404 is the document or the label gone; a 409 a folder label, which only imports assign.
 */
export async function setDocumentLabel(
  api: Api,
  documentId: string,
  labelId: string,
  assigned: boolean,
): Promise<AssignResult> {
  const options = { params: { path: { document_id: documentId, label_id: labelId } } };
  try {
    const { data, response } = assigned
      ? await api.PUT("/api/documents/{document_id}/labels/{label_id}", options)
      : await api.DELETE("/api/documents/{document_id}/labels/{label_id}", options);
    if (response.status === 409) return { status: "error", error: "not-owners" };
    if (response.status >= 400 && response.status < 500) {
      return { status: "error", error: errorFor(response.status) };
    }
    if (!response.ok || data?.id !== documentId) return { status: "unknown" };
    const carries = data.labels.some(
      (label) => label.id === labelId && label.source === "user" && label.status === "accepted",
    );
    return carries === assigned ? { status: "ok", document: data } : { status: "unknown" };
  } catch {
    return { status: "unknown" };
  }
}

/** What a refused change to the owner's labels says. */
export const LABEL_CHANGE_ERROR_TEXT: Record<CreateError | AssignError, string> = {
  ...ERROR_TEXT,
  "not-found": "The document or the label is no longer in the vault. Reload the page.",
  "bad-name": "A name needs 1 to 255 characters other than spaces.",
  "name-taken": "There is already one of that name. Choose it from the list instead.",
  "not-owners": "Folder labels come from imports and cannot be changed here.",
  unreachable: "Nothing was changed. Reload the page and try again.",
};
