import { useCallback, useEffect, useState } from "react";

import { useApi } from "./api-context";
import type { Api } from "./client";
import {
  type DocumentDetail,
  type DocumentList,
  type DocumentPages,
  type DocumentsError,
  type Order,
  PAGE_SIZE,
  type SortKey,
  TEXT_WINDOW,
  errorFor,
  fetchOriginal,
} from "./documents";
import { useSessionState } from "./session-context";

export type Loaded<T> =
  { status: "loading" } | { status: "ok"; data: T } | { status: "error"; error: DocumentsError };

/** A request the page can repeat, for example after the owner pasted a token. */
export interface Request<T> {
  state: Loaded<T>;
  reload: () => void;
}

/**
 * Run `load` whenever `key` changes, and report only the answer to the latest request: an answer
 * to an older key (the previous page, say) is never shown for the current one.
 *
 * A refused token (401) also asks the shared session again, so the token notice comes back: the
 * token can stop working while the page is open (a new `local-token` after a restart).
 */
function useRequest<T>(key: string, load: () => Promise<Loaded<T>>): Request<T> {
  const [answer, setAnswer] = useState<{ key: string; state: Loaded<T> } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const { recheck } = useSessionState();
  const current = `${String(attempt)}:${key}`;

  useEffect(() => {
    let cancelled = false;
    void load()
      .catch((): Loaded<T> => ({ status: "error", error: "unreachable" }))
      .then((state) => {
        if (cancelled) return;
        setAnswer({ key: current, state });
        if (state.status === "error" && state.error === "needs-token") recheck();
      });
    return () => {
      cancelled = true;
    };
    // `load` is rebuilt on every render; `current` names everything it depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  const reload = useCallback(() => {
    setAttempt((value) => value + 1);
  }, []);
  return { state: answer?.key === current ? answer.state : { status: "loading" }, reload };
}

export interface ListQuery {
  sort: SortKey;
  order: Order;
  page: number;
  trash: boolean;
}

/** One page of the document list, as the backend sorted it. */
export function useDocumentList(query: ListQuery): Request<DocumentList> {
  const api = useApi();
  const key = JSON.stringify([query.sort, query.order, query.page, query.trash]);
  return useRequest(key, async () => {
    const { data, response } = await api.GET("/api/documents", {
      params: {
        query: {
          sort: query.sort,
          order: query.order,
          limit: PAGE_SIZE,
          offset: (query.page - 1) * PAGE_SIZE,
          trash: query.trash,
        },
      },
    });
    return data ? { status: "ok", data } : { status: "error", error: errorFor(response.status) };
  });
}

/**
 * Ask for one document's details. A rejected request is "unreachable"; the caller asks the
 * session again on a 401 (`useRequest` does, and so does the details page after a move).
 */
export async function loadDocument(
  api: Api,
  id: string,
): Promise<Exclude<Loaded<DocumentDetail>, { status: "loading" }>> {
  try {
    const { data, response } = await api.GET("/api/documents/{document_id}", {
      params: { path: { document_id: id } },
    });
    return data ? { status: "ok", data } : { status: "error", error: errorFor(response.status) };
  } catch {
    return { status: "error", error: "unreachable" };
  }
}

/**
 * A window of the extracted text of a document's current version, from `offset` pages in. A 500
 * is the backend's answer for stored text that breaks the page rules, so it is "unavailable".
 */
export function useDocumentPages(id: string, offset: number): Request<DocumentPages> {
  const api = useApi();
  return useRequest(JSON.stringify([id, offset]), async () => {
    const { data, response } = await api.GET("/api/documents/{document_id}/pages", {
      params: { path: { document_id: id }, query: { offset, limit: TEXT_WINDOW } },
    });
    if (data) return { status: "ok", data };
    return {
      status: "error",
      error: response.status === 500 ? "unavailable" : errorFor(response.status),
    };
  });
}

/** One document's details. */
export function useDocument(id: string): Request<DocumentDetail> {
  const api = useApi();
  return useRequest(id, () => loadDocument(api, id));
}

/** A PDF's bytes, as PDF.js takes them. */
export interface PdfFile {
  data: Uint8Array;
}

/**
 * The bytes of a document's original file, for the PDF viewer (`fetchOriginal`: the token goes in
 * the header, never in the address). `sha256` names the bytes, so a new current version is
 * fetched again; the same bytes are not fetched again when the page shown changes.
 */
export function useOriginalPdf(id: string, sha256: string): Request<PdfFile> {
  const api = useApi();
  return useRequest(JSON.stringify([id, sha256]), async () => {
    const original = await fetchOriginal(api, id);
    if (original.status === "error") return original;
    return { status: "ok", data: { data: new Uint8Array(await original.blob.arrayBuffer()) } };
  });
}
