import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";

import { fetchHealth } from "../api/health";
import { HealthContext } from "../context/healthContext";
import type { HealthState } from "../context/healthContext";
import type { HealthStatus } from "../types/health";

/**
 * Fetches the app's health/liveness status (`GET /health`, COL-38) once and
 * shares it with every consumer beneath it via `useHealth()`.
 *
 * COL-124's code review flagged that `Sidebar`'s version footer fetched its
 * own independent copy of `GET /health` even though `HealthBanner` (mounted
 * as a sibling in `AppShell`) already fetches it on every page load -- the
 * same class of duplicate-request problem `InstancesProvider` (COL-100 code
 * review) fixed for `GET /api/instances`. Mounted once in `AppShell`, above
 * both `Sidebar` and `HealthBanner`, so the whole app makes that request
 * once instead of each consumer re-fetching independently.
 *
 * COL-222 adds `refresh()` (mirrors `UpdatesProvider`, COL-196 code review):
 * after the health banner's opt-in "Download FFmpeg" action succeeds, the
 * `ffmpeg_missing` check is already fixed server-side (COL-218 reads
 * `GlobalSettings.ffmpeg_path` live on every tick) -- `refresh()` re-fetches
 * `/health` immediately afterwards so the banner reflects that without
 * waiting for a page reload or the next unrelated fetch.
 */
export function HealthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<HealthState>({ status: "loading" });

  const refresh = useCallback(async () => {
    const health = await fetchHealth();
    setState({ status: "ready", health });
    return health;
  }, []);

  useEffect(() => {
    let cancelled = false;

    fetchHealth()
      .then((health: HealthStatus) => {
        if (!cancelled) setState({ status: "ready", health });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return <HealthContext.Provider value={{ state, refresh }}>{children}</HealthContext.Provider>;
}
