import { useContext } from "react";

import { HealthContext } from "../context/healthContext";
import type { HealthState } from "../context/healthContext";

export type { HealthState };

/** Reads the shared health status. Must be rendered beneath `HealthProvider`. */
export function useHealth(): HealthState {
  const state = useContext(HealthContext);
  if (state === undefined) {
    throw new Error("useHealth must be used within a HealthProvider");
  }
  return state;
}
