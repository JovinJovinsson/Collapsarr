import { useEffect, useState } from "react";

import { fetchHealth } from "../api/health";
import type { HealthStatus } from "../types/health";
import { WarningIcon } from "./icons";

/**
 * App-wide health warning banner (COL-38). Fetches `GET /health` once on
 * mount and, when the app reports itself "degraded" (currently: FFmpeg
 * missing at startup, `collapsarr/health.py`), renders a persistent warning
 * above every view -- rendered in `AppShell` so it's visible regardless of
 * which page the user is on. Renders nothing when the app is healthy, when
 * the fetch hasn't resolved yet, or if the fetch itself fails (a transient
 * network hiccup shouldn't itself read as an alarming health warning).
 */
export function HealthBanner() {
  const [health, setHealth] = useState<HealthStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchHealth()
      .then((result) => {
        if (!cancelled) {
          setHealth(result);
        }
      })
      .catch(() => {
        // Best-effort: see the docstring above.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!health || health.status !== "degraded" || health.warnings.length === 0) {
    return null;
  }

  return (
    <div className="health-banner" role="alert">
      {health.warnings.map((warning) => (
        <p key={warning.code} className="health-banner__message">
          <WarningIcon className="health-banner__icon" />
          {warning.message}
        </p>
      ))}
    </div>
  );
}
