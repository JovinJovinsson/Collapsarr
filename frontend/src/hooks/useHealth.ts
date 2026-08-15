import { useContext } from "react";

import { HealthContext } from "../context/healthContext";
import type { HealthContextValue, HealthState } from "../context/healthContext";

export type { HealthContextValue, HealthState };

/**
 * Reads the shared health status plus a `refresh()` to re-fetch it on demand
 * (COL-222). Must be rendered beneath `HealthProvider`.
 */
export function useHealth(): HealthContextValue {
  const value = useContext(HealthContext);
  if (value === undefined) {
    throw new Error("useHealth must be used within a HealthProvider");
  }
  return value;
}
