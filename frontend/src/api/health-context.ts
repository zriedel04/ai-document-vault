import { createContext, useContext } from "react";

import type { HealthState } from "./useHealth";

const HealthContext = createContext<HealthState>({ status: "loading" });

/** Gives every route the backend health that `App` fetched once. */
export const HealthProvider = HealthContext.Provider;

export function useHealthState(): HealthState {
  return useContext(HealthContext);
}
