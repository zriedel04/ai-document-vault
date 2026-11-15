import { useApi } from "./api-context";
import { errorFor } from "./documents";
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
