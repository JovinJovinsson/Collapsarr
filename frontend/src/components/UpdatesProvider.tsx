import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { fetchUpdateStatus, recheckUpdateStatus } from "../api/updates";
import { UpdatesContext } from "../context/updatesContext";
import type { UpdateCheckState } from "../types/updates";

/**
 * Fetches the Update Check state (`GET /api/system/updates`, COL-87) once
 * and shares it with every consumer beneath it via `useUpdates()`, mirroring
 * `HealthProvider`/`useHealth` (COL-124 code review) and
 * `InstancesProvider`/`useInstances` (COL-100 code review).
 *
 * COL-196 code review: `UpdateIndicator` previously fetched its own copy on
 * mount and never refreshed it again for the rest of the SPA session (no
 * polling, no listener for external changes) -- so `GeneralSection`'s
 * out-of-band recheck after a release-channel-changing save updated the
 * server's stored state, but the app-wide indicator (mounted once in
 * `AppShell`, alongside the `<Outlet />`) kept showing whatever it fetched
 * on its own one-time mount until a full page reload. Lifting the fetch here
 * and exposing `refresh()` lets a consumer push a freshly rechecked result
 * into the state every mounted consumer reads, without a reload.
 */
export function UpdatesProvider({ children }: { children: ReactNode }) {
  const [update, setUpdate] = useState<UpdateCheckState | null>(null);

  useEffect(() => {
    let cancelled = false;

    fetchUpdateStatus()
      .then((result) => {
        if (!cancelled) setUpdate(result);
      })
      .catch(() => {
        // Best-effort, same as the fetch this replaces in `UpdateIndicator`:
        // a transient failure shouldn't itself read as a notice -- it just
        // leaves `update` as `null` until the next successful fetch.
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = useCallback(async () => {
    const result = await recheckUpdateStatus();
    setUpdate(result);
    return result;
  }, []);

  return <UpdatesContext.Provider value={{ update, refresh }}>{children}</UpdatesContext.Provider>;
}
