import { Outlet } from "react-router-dom";

import { HealthBanner } from "./HealthBanner";
import { OnboardingPanel } from "./OnboardingPanel";
import { Sidebar } from "./Sidebar";

/**
 * Top-level layout: a fixed sidebar on the left and a scrollable content
 * region on the right. Individual views render into the <Outlet />.
 *
 * `HealthBanner` (COL-38) sits above the outlet so an app-health warning
 * (currently: FFmpeg missing at startup) is visible no matter which view is
 * active, rather than tucked away on a single page.
 *
 * `OnboardingPanel` (COL-54) sits below it, same rationale: it should be
 * visible regardless of which view a freshly-set-up install lands on, until
 * dismissed or the install is configured (an arr instance exists).
 */
export function AppShell() {
  return (
    <div className="app-shell">
      <Sidebar />
      <main className="app-shell__content" id="main-content">
        <HealthBanner />
        <OnboardingPanel />
        <Outlet />
      </main>
    </div>
  );
}
