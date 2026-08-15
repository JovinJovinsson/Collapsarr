import { createContext } from "react";

import type { HealthStatus } from "../types/health";

export type HealthState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; health: HealthStatus };

export interface HealthContextValue {
  state: HealthState;
  /**
   * Re-fetches `GET /health` and pushes the result into this shared state
   * (COL-222), mirroring `UpdatesContextValue.refresh`
   * (`context/updatesContext.ts`, COL-196 code review): lets a consumer that
   * just changed something the health check cares about -- the FFmpeg
   * auto-download button, `HealthBanner.tsx` -- push a freshly refetched
   * state into every mounted `useHealth()` consumer immediately, without a
   * page reload. Rejects on failure, leaving the previously known state in
   * place, same as `UpdatesContextValue.refresh`.
   */
  refresh: () => Promise<HealthStatus>;
}

/**
 * Backs `HealthProvider`/`useHealth()` (COL-124 code review, `refresh` added
 * COL-222): the shared `GET /health` fetch/state that `HealthBanner` and
 * `Sidebar`'s version footer both read instead of each re-fetching
 * independently. Split into its own plain module (no JSX/component export)
 * so `HealthProvider` and `useHealth` can each live in their own
 * single-export file for Fast Refresh -- mirrors
 * `instancesContext.ts`/`InstancesProvider`, which fixed the same class of
 * duplicate-fetch problem for `GET /api/instances`.
 */
export const HealthContext = createContext<HealthContextValue | undefined>(undefined);
