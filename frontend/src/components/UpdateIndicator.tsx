import { Download } from "lucide-react";
import { Link } from "react-router-dom";

import { useUpdates } from "../hooks/useUpdates";

/**
 * App-wide "update available" indicator (COL-87). Reads the shared Update
 * Check state from `UpdatesProvider` (mounted in `AppShell`, above this
 * component) via `useUpdates()` and, when an update is available, renders a
 * small persistent strip above every view -- alongside `HealthBanner`/
 * `OnboardingPanel` so it's visible regardless of which page the user is on.
 * Renders nothing while up to date, while the initial fetch hasn't resolved
 * yet, or if it failed (a transient network hiccup shouldn't itself read as
 * a notice).
 *
 * COL-196 code review: previously fetched `GET /api/system/updates` itself,
 * once on mount, with no way to pick up a fresher result for the rest of the
 * SPA session. Reading from `UpdatesProvider` instead means any consumer
 * that pushes a refreshed result into the shared state (e.g. `GeneralSection`
 * after a release-channel-changing save, via `useUpdates().refresh()`) is
 * reflected here immediately, without a page reload.
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
  const { update } = useUpdates();

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
