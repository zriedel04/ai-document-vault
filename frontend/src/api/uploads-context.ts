import { createContext, useContext } from "react";

import type { Uploads } from "./useUploads";

const UploadsContext = createContext<Uploads | null>(null);

/** Gives every route the upload queue that `App` keeps, so it survives a change of page. */
export const UploadsProvider = UploadsContext.Provider;

export function useUploadsState(): Uploads {
  const uploads = useContext(UploadsContext);
  if (uploads === null) throw new Error("useUploadsState needs an UploadsProvider");
  return uploads;
}
