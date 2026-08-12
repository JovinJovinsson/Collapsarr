import { useContext } from "react";

import { UpdatesContext } from "../context/updatesContext";
import type { UpdatesContextValue } from "../context/updatesContext";

export type { UpdatesContextValue };

/** Reads the shared Update Check state. Must be rendered beneath `UpdatesProvider`. */
export function useUpdates(): UpdatesContextValue {
  const value = useContext(UpdatesContext);
  if (value === undefined) {
    throw new Error("useUpdates must be used within an UpdatesProvider");
  }
  return value;
}
