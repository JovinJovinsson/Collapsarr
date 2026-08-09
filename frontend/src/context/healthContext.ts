import { createContext } from "react";

import type { HealthStatus } from "../types/health";

export type HealthState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; health: HealthStatus };

/**
 * Backs `HealthProvider`/`useHealth()` (COL-124 code review): the shared
 * `GET /health` fetch/state that `HealthBanner` and `Sidebar`'s version
 * footer both read instead of each re-fetching independently. Split into
 * its own plain module (no JSX/component export) so `HealthProvider` and
 * `useHealth` can each live in their own single-export file for Fast
 * Refresh -- mirrors `instancesContext.ts`/`InstancesProvider`, which fixed
 * the same class of duplicate-fetch problem for `GET /api/instances`.
 */
export const HealthContext = createContext<HealthState | undefined>(undefined);
