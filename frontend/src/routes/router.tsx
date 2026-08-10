import { createBrowserRouter, Navigate, type RouteObject } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { FileDetailPage } from "../pages/FileDetailPage";
import { LibraryPage } from "../pages/LibraryPage";
import { LoginPage } from "../pages/LoginPage";
import { SetupPage } from "../pages/SetupPage";
import { getUrlBase } from "../runtime/urlBase";
import { navItems, settingsNavItems, SYSTEM_PATH, systemNavItems } from "./nav";

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
      // Settings sub-nav (COL-142/COL-143): General, Targets, and Connect
      // are migrated. The old composed `/settings` route (wired via
      // `navItems` above) still serves Instances and stays reachable -- no
      // bare /settings -> /settings/general redirect yet (that ships with
      // the last migration, COL-144).
      ...settingsNavItems.map(({ to, element }) => ({ path: to, element })),
      // Per-file detail (COL-34): not a primary nav destination, so it's
      // wired directly here rather than through `navItems` (the sidebar's
      // source of truth) -- it's reached from a file row, not the sidebar.
      { path: "/wanted/:fileId", element: <FileDetailPage /> },
      // Per-instance Library browsing (COL-100): `navItems` only wires the
      // bare `/libraries` redirect (`LibrariesIndexPage`); the per-instance
      // tree view takes an id param, so it's wired directly here, same as
      // `/wanted/:fileId` above. Reached from the sidebar's expanded
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
