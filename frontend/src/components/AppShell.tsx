import { Outlet } from "react-router-dom";

import { Sidebar } from "./Sidebar";

/**
 * Top-level layout: a fixed sidebar on the left and a scrollable content
 * region on the right. Individual views render into the <Outlet />.
 */
export function AppShell() {
  return (
    <div className="app-shell">
      <Sidebar />
      <main className="app-shell__content" id="main-content">
        <Outlet />
      </main>
    </div>
  );
}
