import { useCallback, useEffect, useState } from "react";

import type { Api } from "./client";
import { readToken } from "./token";

/** Whether this browser may use the API, and if not, why. */
export type SessionState =
  | { status: "loading" }
  | { status: "signed-in"; name: string }
  | { status: "needs-token"; reason: "missing" | "refused" }
  | { status: "unreachable" };

export interface Session {
  state: SessionState;
  /** Ask again, for example after the owner pasted a token. */
  recheck: () => void;
}

/** Ask the backend who this browser is. Without a stored token no request is made at all. */
export function useSession(api: Api): Session {
  // An answer belongs to the attempt that asked for it, so an older answer is never shown for a
  // newer attempt while that one is still waiting.
  const [answer, setAnswer] = useState<{ attempt: number; state: SessionState } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const hasToken = readToken() !== null;

  useEffect(() => {
    if (!hasToken) return;
    let cancelled = false;
    const settle = (state: SessionState) => {
      if (!cancelled) setAnswer({ attempt, state });
    };
    api
      .GET("/api/session")
      .then(({ data, response }) => {
        if (data) settle({ status: "signed-in", name: data.name });
        else if (response.status === 401) settle({ status: "needs-token", reason: "refused" });
        else settle({ status: "unreachable" });
      })
      .catch(() => {
        settle({ status: "unreachable" });
      });
    return () => {
      cancelled = true;
    };
  }, [api, attempt, hasToken]);

  const recheck = useCallback(() => {
    setAttempt((value) => value + 1);
  }, []);

  let state: SessionState = { status: "loading" };
  if (!hasToken) state = { status: "needs-token", reason: "missing" };
  else if (answer?.attempt === attempt) state = answer.state;
  return { state, recheck };
}
