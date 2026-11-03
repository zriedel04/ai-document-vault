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
} from "./documents";
import { useSessionState } from "./session-context";

/** A request's state. `E` is why it gave no answer: a documents error unless a caller adds more. */
export type Loaded<T, E extends string = DocumentsError> =
  { status: "loading" } | { status: "ok"; data: T } | { status: "error"; error: E };

/** A request the page can repeat, for example after the owner pasted a token. */
export interface Request<T, E extends string = DocumentsError> {
  state: Loaded<T, E>;
  reload: () => void;
}

/**
 * Run `load` whenever `key` changes, and report only the answer to the latest request: an answer
 * to an older key (the previous page, say) is never shown for the current one. The key is React
 * state only, so a private value in it (a search query) never reaches the address.
 *
 * A refused token (401) also asks the shared session again, so the token notice comes back: the
 * token can stop working while the page is open (a new `local-token` after a restart).
 */
export function useRequest<T, E extends string = DocumentsError>(
  key: string,
  load: () => Promise<Loaded<T, E>>,
): Request<T, E | "unreachable"> {
  type State = Loaded<T, E | "unreachable">;
  const [answer, setAnswer] = useState<{ key: string; state: State } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const { recheck } = useSessionState();
  const current = `${String(attempt)}:${key}`;

  useEffect(() => {
    let cancelled = false;
    void load()
      .catch((): State => ({ status: "error", error: "unreachable" }))
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
