import { Download } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchUpdateStatus } from "../api/updates";
import type { UpdateCheckState } from "../types/updates";

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
 *
 * COL-89: also hides once the notice is dismissed (`dismissed_at` set),
 * mirroring `HealthBanner`'s treatment of a dismissed health check -- the
 * ambient, every-page nag goes away, while `UpdatesPage` (the dedicated
 * System > Updates view) still shows the update, marked dismissed, with an
 * Undismiss action. The server auto-clears the dismissal the moment a newer
 * release is published, so this indicator reappears on its own then.
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

  if (!update || !update.update_available || update.dismissed_at) {
    return null;
  }

  return (
    <Link to="/system/updates" className="update-indicator" role="status">
      <Download className="update-indicator__icon" width={16} height={16} />
      <span className="update-indicator__message">
        Update available{update.latest_version ? ` — ${update.latest_version}` : ""}
      </span>
    </Link>
  );
}
