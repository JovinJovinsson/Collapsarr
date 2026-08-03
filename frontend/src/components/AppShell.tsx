import { Outlet } from "react-router-dom";

import { HealthBanner } from "./HealthBanner";
import { OnboardingPanel } from "./OnboardingPanel";
import { Sidebar } from "./Sidebar";
import { UpdateIndicator } from "./UpdateIndicator";

/**
 * Top-level layout: a fixed sidebar on the left and a scrollable content
 * region on the right. Individual views render into the <Outlet />.
 *
 * `HealthBanner` (COL-38) sits above the outlet so an app-health warning
 * (currently: FFmpeg missing at startup) is visible no matter which view is
 * active, rather than tucked away on a single page.
 *
 * `UpdateIndicator` (COL-87) sits alongside it: a small, neutral notice when
 * a newer release is available, deliberately styled unlike `HealthBanner` --
 * nothing is broken, so it doesn't compete visually with a real health
 * warning.
 *
 * `OnboardingPanel` (COL-54) sits below both, same rationale: it should be
 * visible regardless of which view a freshly-set-up install lands on, until
 * dismissed or the install is configured (an arr instance exists).
 */
export function AppShell() {
  return (
    <div className="app-shell">
      <Sidebar />
      <main className="app-shell__content" id="main-content">
        <HealthBanner />
        <UpdateIndicator />
        <OnboardingPanel />
        <Outlet />
      </main>
    </div>
  );
}
