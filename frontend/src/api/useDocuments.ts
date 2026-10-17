import { useCallback, useEffect, useState } from "react";

import { useApi } from "./api-context";
import {
  type DocumentDetail,
  type DocumentList,
  type DocumentsError,
  type Order,
  PAGE_SIZE,
  type SortKey,
  errorFor,
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

/** One document's details. */
export function useDocument(id: string): Request<DocumentDetail> {
  const api = useApi();
  return useRequest(id, async () => {
    const { data, response } = await api.GET("/api/documents/{document_id}", {
      params: { path: { document_id: id } },
    });
    return data ? { status: "ok", data } : { status: "error", error: errorFor(response.status) };
  });
}
