import { useEffect, useState } from "react";

import { fetchHealth } from "../api/health";
import type { HealthStatus, HealthWarning } from "../types/health";
import { ErrorIcon, WarningIcon } from "./icons";

/** Icon per severity -- error gets the octagon, warning the triangle (COL-76). */
const SEVERITY_ICON: Record<HealthWarning["severity"], typeof WarningIcon> = {
  warning: WarningIcon,
  error: ErrorIcon,
};

/**
 * App-wide health warning banner (COL-38). Fetches `GET /health` once on
 * mount and, when the app reports itself "degraded" (one or more registered
 * checks currently failing, `collapsarr/health/`), renders a persistent
 * banner above every view -- rendered in `AppShell` so it's visible
 * regardless of which page the user is on. Renders nothing when the app is
 * healthy, when the fetch hasn't resolved yet, or if the fetch itself fails
 * (a transient network hiccup shouldn't itself read as an alarming health
 * warning).
 *
 * COL-76 distinguishes each entry's severity (`warning` vs `error`, per
 * `/health`'s `severity` field) with a different icon/colour -- previously
 * every entry rendered identically regardless of how many checks were failing
 * or how severe each one was. A single mixed-severity list still renders as
 * one banner (not one per severity) so it stays a single glance-able summary;
 * only the per-row icon/colour differs.
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
      {health.warnings.map((warning) => {
        // Defensive default (rather than trusting the value blindly): an
        // unexpected/missing severity -- e.g. a stale cached response from
        // before COL-76 added the field -- still renders, just as a warning.
        const severity: HealthWarning["severity"] = warning.severity === "error" ? "error" : "warning";
        const Icon = SEVERITY_ICON[severity];
        return (
          <p key={warning.code} className={`health-banner__message health-banner__message--${severity}`}>
            <Icon className="health-banner__icon" />
            {warning.message}
          </p>
        );
      })}
    </div>
  );
}
