import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { fetchInstances } from "../api/instances";
import { InstancesContext } from "../context/instancesContext";
import type { InstancesState } from "../context/instancesContext";

/**
 * Fetches the configured Sonarr/Radarr instance list (`GET /api/instances`)
 * once and shares it with every consumer beneath it via `useInstances()`.
 *
 * COL-100's code review flagged that `fetchInstances()` had grown 3
 * independent hand-rolled loading/error state machines across
 * `LibraryNavSection` (in `Sidebar`, always mounted), `LibrariesIndexPage`,
 * and `LibraryPage` -- so e.g. visiting `/libraries/1` fired two identical
 * `GET /api/instances` requests concurrently (the sidebar's copy and the
 * page's own). Mounted once in `AppShell`, above both the sidebar and the
 * routed `<Outlet />`, so both share this single fetch/state instead.
 *
 * The Settings page's `InstancesSection` intentionally stays on its own
 * `fetchInstances()` call rather than this provider: it owns create/update/
 * delete/reload of instances, which this read-only shared state doesn't
 * (and doesn't need to for the Libraries browsing views).
 */
export function InstancesProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<InstancesState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    fetchInstances()
      .then((instances) => {
        if (!cancelled) setState({ status: "ready", instances });
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

  return <InstancesContext.Provider value={state}>{children}</InstancesContext.Provider>;
}
