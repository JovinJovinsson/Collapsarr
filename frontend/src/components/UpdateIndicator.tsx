import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchUpdateStatus } from "../api/updates";
import type { UpdateCheckState } from "../types/updates";
import { UpdateIcon } from "./icons";

/**
 * App-wide "update available" indicator (COL-87). Fetches `GET /api/system/
 * updates` once on mount and, when an update is available, renders a small
 * persistent strip above every view -- rendered in `AppShell` alongside
 * `HealthBanner`/`OnboardingPanel` so it's visible regardless of which page
 * the user is on. Renders nothing while up to date, while the fetch hasn't
 * resolved yet, or if the fetch itself fails (a transient network hiccup
 * shouldn't itself read as a notice).
 *
 * Deliberately **not** styled like `HealthBanner`: nothing is broken here --
 * an available update is informational (`CONTEXT.md`'s Update Check entry has
 * no severity, no Check Code), so this uses the app's neutral accent tone
 * (`.update-indicator`, matching `OnboardingPanel`'s accent-tinted shell)
 * rather than `HealthBanner`'s danger-toned one. Links to `/system/updates`
 * so an operator can see the details (running vs. latest version, changelog).
 */
export function UpdateIndicator() {
  const [update, setUpdate] = useState<UpdateCheckState | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchUpdateStatus()
      .then((result) => {
        if (!cancelled) {
          setUpdate(result);
        }
      })
      .catch(() => {
        // Best-effort: see the docstring above.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!update || !update.update_available) {
    return null;
  }

  return (
    <Link to="/system/updates" className="update-indicator" role="status">
      <UpdateIcon className="update-indicator__icon" width={16} height={16} />
      <span className="update-indicator__message">
        Update available{update.latest_version ? ` — ${update.latest_version}` : ""}
      </span>
    </Link>
  );
}
