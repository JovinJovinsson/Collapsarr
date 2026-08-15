import { Outlet } from "react-router-dom";

import { HealthBanner } from "./HealthBanner";
import { HealthProvider } from "./HealthProvider";
import { InstancesProvider } from "./InstancesProvider";
import { OnboardingPanel } from "./OnboardingPanel";
import { Sidebar } from "./Sidebar";
import { UpdateIndicator } from "./UpdateIndicator";
import { UpdatesProvider } from "./UpdatesProvider";

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
 *
 * `InstancesProvider` (COL-100 code review) wraps both `Sidebar` and the
 * `<Outlet />`: it's the shared `GET /api/instances` fetch that `Sidebar`'s
 * `LibraryNavSection` and the Libraries pages (`LibrariesIndexPage`,
 * `LibraryPage`) all read via `useInstances()`, so mounting it here -- above
 * both -- means the whole app makes that request once instead of each
 * consumer re-fetching independently.
 *
 * `HealthProvider` (COL-124 code review) wraps the same subtree for the same
 * reason: it's the shared `GET /health` fetch that `HealthBanner` and
 * `Sidebar`'s version footer both read via `useHealth()`, so the app makes
 * that request once instead of each consumer re-fetching independently.
 *
 * `UpdatesProvider` (COL-196 code review) wraps `<Outlet />` too, not just
 * `UpdateIndicator`: it's the shared Update Check state `UpdateIndicator`
 * reads via `useUpdates()`, and also exposes `refresh()` so a page rendered
 * into the outlet (currently `GeneralSection`, after a release-channel
 * change) can push a freshly rechecked result into that shared state --
 * `UpdateIndicator` then reflects it immediately, without a page reload.
 */
export function AppShell() {
  return (
    <HealthProvider>
      <InstancesProvider>
        <UpdatesProvider>
          <div className="app-shell">
            <Sidebar />
            <main className="app-shell__content" id="main-content">
              <HealthBanner />
              <UpdateIndicator />
              <OnboardingPanel />
              <Outlet />
            </main>
          </div>
        </UpdatesProvider>
      </InstancesProvider>
    </HealthProvider>
  );
}
