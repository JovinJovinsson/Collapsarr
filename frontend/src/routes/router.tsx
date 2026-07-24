import { createBrowserRouter, Navigate } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { FileDetailPage } from "../pages/FileDetailPage";
import { LoginPage } from "../pages/LoginPage";
import { SetupPage } from "../pages/SetupPage";
import { navItems } from "./nav";

export const router = createBrowserRouter([
  // Auth screens (COL-50) live outside the AppShell layout: no sidebar, no
  // session required. The server's enforcement middleware redirects UI routes
  // to /setup (first run) or /login (no session) so these are reachable before
  // a session exists; they render standalone here.
  { path: "/setup", element: <SetupPage /> },
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/wanted" replace /> },
      ...navItems.map(({ to, element }) => ({ path: to, element })),
      // Per-file detail (COL-34): not a primary nav destination, so it's
      // wired directly here rather than through `navItems` (the sidebar's
      // source of truth) -- it's reached from a file row, not the sidebar.
      { path: "/wanted/:fileId", element: <FileDetailPage /> },
      { path: "*", element: <Navigate to="/wanted" replace /> },
    ],
  },
]);
