import { useEffect, useState } from "react";

import type { Api } from "./client";

export type HealthState =
  { status: "loading" } | { status: "ok"; version: string } | { status: "unreachable" };

/** Ask the backend for its health once, and report whether it answered. */
export function useHealth(api: Api): HealthState {
  const [state, setState] = useState<HealthState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    api
      .GET("/api/health")
      .then(({ data }) => {
        if (cancelled) return;
        setState(data ? { status: "ok", version: data.version } : { status: "unreachable" });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "unreachable" });
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  return state;
}
