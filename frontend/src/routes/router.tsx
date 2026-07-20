import { createBrowserRouter, Navigate } from "react-router-dom";

import { AppShell } from "../components/AppShell";
import { navItems } from "./nav";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/wanted" replace /> },
      ...navItems.map(({ to, element }) => ({ path: to, element })),
      { path: "*", element: <Navigate to="/wanted" replace /> },
    ],
  },
]);
