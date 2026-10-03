import { createContext, useContext } from "react";

import type { Session } from "./useSession";

const SessionContext = createContext<Session>({
  state: { status: "loading" },
  recheck: () => undefined,
});

/** Gives every route the session that `App` asked the backend for. */
export const SessionProvider = SessionContext.Provider;

export function useSessionState(): Session {
  return useContext(SessionContext);
}
