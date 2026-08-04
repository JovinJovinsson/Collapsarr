import { createContext } from "react";

import type { ArrInstance } from "../types/instances";

export type InstancesState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; instances: ArrInstance[] };

/**
 * Backs `InstancesProvider`/`useInstances()` (COL-100 code review): the
 * shared `GET /api/instances` fetch/state that `Sidebar`'s
 * `LibraryNavSection`, `LibrariesIndexPage`, and `LibraryPage` all read
 * instead of each re-fetching independently. Split into its own plain
 * module (no JSX/component export) so `InstancesProvider` and `useInstances`
 * can each live in their own single-export file for Fast Refresh.
 */
export const InstancesContext = createContext<InstancesState | undefined>(undefined);
