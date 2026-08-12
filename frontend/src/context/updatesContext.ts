import { createContext } from "react";

import type { UpdateCheckState } from "../types/updates";

export interface UpdatesContextValue {
  /**
   * Latest known Update Check state, or `null` before the initial fetch
   * resolves (or if it failed). Deliberately *not* a `{status:"loading"|
   * "error"|"ready"}` union like `HealthState`/`InstancesState` -- an
   * available update is informational, not a failure state (`CONTEXT.md`'s
   * Update Check entry), so `UpdateIndicator` (the only consumer today) has
   * nothing distinct to render for "loading" or "errored" beyond what it
   * already does for `null`: render nothing.
   */
  update: UpdateCheckState | null;
  /**
   * Runs an out-of-band recheck (`POST /api/system/updates/recheck`,
   * `recheckUpdateStatus` in `api/updates.ts`) and pushes the result into
   * this shared state, so every mounted consumer of `useUpdates()` (e.g.
   * `UpdateIndicator`) reflects it immediately rather than waiting for its
   * own next fetch. Mirrors `recheckUpdateStatus` in shape -- rejects on
   * failure, leaving the previously known state in place, so a caller that
   * doesn't care about the failure can `.catch(() => undefined)`.
   */
  refresh: () => Promise<UpdateCheckState>;
}

/**
 * Backs `UpdatesProvider`/`useUpdates()` (COL-196 code review): the shared
 * `GET /api/system/updates` fetch/state that `UpdateIndicator` reads instead
 * of fetching its own independent copy, plus a `refresh()` other consumers
 * (e.g. `GeneralSection`, after a release-channel-changing save) can call to
 * push a freshly rechecked result into that shared state. Split into its own
 * plain module (no JSX/component export) so `UpdatesProvider` and
 * `useUpdates` can each live in their own single-export file for Fast
 * Refresh -- same file-layout convention as `healthContext.ts`/
 * `HealthProvider` and `instancesContext.ts`/`InstancesProvider` (see
 * `UpdatesContextValue.update`'s docstring for where the shape intentionally
 * differs from those two).
 */
export const UpdatesContext = createContext<UpdatesContextValue | undefined>(undefined);
