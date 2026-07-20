import { createBrowserRouter, Navigate } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { FileDetailPage } from "../pages/FileDetailPage";
import { navItems } from "./nav";

export const router = createBrowserRouter([
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
