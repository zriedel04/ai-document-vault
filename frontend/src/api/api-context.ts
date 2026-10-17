import { createContext, useContext } from "react";

import type { Api } from "./client";

const ApiContext = createContext<Api | null>(null);

/** Gives every route the API client that `App` was created with. */
export const ApiProvider = ApiContext.Provider;

export function useApi(): Api {
  const api = useContext(ApiContext);
  if (api === null) throw new Error("useApi needs an ApiProvider");
  return api;
}
