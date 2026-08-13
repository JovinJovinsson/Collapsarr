import { createBrowserRouter, Navigate, type RouteObject } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { WantedFileRedirect } from "../components/WantedFileRedirect";
import { FileDetailPage } from "../pages/FileDetailPage";
import { LibraryPage } from "../pages/LibraryPage";
import { LoginPage } from "../pages/LoginPage";
import { SetupPage } from "../pages/SetupPage";
import { getUrlBase } from "../runtime/urlBase";
import {
  navItems,
  SETTINGS_GENERAL_PATH,
  SETTINGS_PATH,
  settingsNavItems,
  SYSTEM_PATH,
  systemNavItems,
} from "./nav";

/**
 * Route config, exported separately from `router` (below) so tests can drive
 * the *actual* production route tree -- e.g. the bare `/system` redirect's
 * `<Navigate>` element -- through `createMemoryRouter(routes, ...)` rather
 * than re-declaring a parallel fixture that can drift from what's shipped
 * (COL-126 code review).
 */
export const routes: RouteObject[] = [
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
      // System area (COL-63): its pages, plus a bare /system that lands on the
      // first System view (Tasks).
      { path: SYSTEM_PATH, element: <Navigate to={`${SYSTEM_PATH}/tasks`} replace /> },
      ...systemNavItems.map(({ to, element }) => ({ path: to, element })),
      // Settings sub-nav (COL-142/COL-143/COL-144): every section has now
      // migrated off the old composed `SettingsPage` -- General, Sonarr,
      // Radarr, Targets, Connect. Mirrors the bare `/system` redirect above:
      // a bare `/settings` lands on the sub-nav's first entry (General).
      { path: SETTINGS_PATH, element: <Navigate to={SETTINGS_GENERAL_PATH} replace /> },
      ...settingsNavItems.map(({ to, element }) => ({ path: to, element })),
      // Per-file detail (COL-34, COL-203): not a primary nav destination, so
      // it's wired directly here rather than through `navItems` (the
      // sidebar's source of truth) -- it's reached from a file row, not the
      // sidebar. `/wanted/:fileId` is the pre-COL-203 path, kept as a
      // redirect to the new `/files/:fileId` below.
      { path: "/files/:fileId", element: <FileDetailPage /> },
      { path: "/wanted/:fileId", element: <WantedFileRedirect /> },
      // Per-instance Library browsing (COL-100): `navItems` only wires the
      // bare `/libraries` redirect (`LibrariesIndexPage`); the per-instance
      // tree view takes an id param, so it's wired directly here, same as
      // `/files/:fileId` above. Reached from the sidebar's expanded
      // Libraries sub-items or the index redirect, not a plain nav link.
      { path: "/libraries/:instanceId", element: <LibraryPage /> },
      { path: "*", element: <Navigate to="/wanted" replace /> },
    ],
  },
];

export const router = createBrowserRouter(
  routes,
  // Mount the client-side router under the reverse-proxy subpath (COL-118) so
  // every route resolves correctly when Collapsarr is served at e.g.
  // `/collapsarr/`. `undefined` (no configured base) is React Router's default
  // root mount -- today's behaviour. React Router itself strips the basename
  // from `useNavigate`/`<Link>` targets, so the sidebar's `navigate("/login")`
  // and the route `path`s above stay written at the root and are prefixed here.
  { basename: getUrlBase() || undefined },
);
