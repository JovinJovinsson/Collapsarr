import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { fetchHealth } from "../api/health";
import { HealthContext } from "../context/healthContext";
import type { HealthState } from "../context/healthContext";

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
 */
export function HealthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<HealthState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    fetchHealth()
      .then((health) => {
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

  return <HealthContext.Provider value={state}>{children}</HealthContext.Provider>;
}
