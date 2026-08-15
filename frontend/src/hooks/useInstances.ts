import { useContext } from "react";

import { InstancesContext } from "../context/instancesContext";
import type { InstancesState } from "../context/instancesContext";

export type { InstancesState };

/** Reads the shared instance list. Must be rendered beneath `InstancesProvider`. */
export function useInstances(): InstancesState {
  const state = useContext(InstancesContext);
  if (state === undefined) {
    throw new Error("useInstances must be used within an InstancesProvider");
  }
  return state;
}
