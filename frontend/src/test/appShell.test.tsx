import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { AppShell } from "../components/AppShell";
import { ActivityPage } from "../pages/ActivityPage";
import { SettingsPage } from "../pages/SettingsPage";
import { WantedPage } from "../pages/WantedPage";

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <AppShell />,
        children: [
          { path: "wanted", element: <WantedPage /> },
          { path: "activity", element: <ActivityPage /> },
          { path: "settings", element: <SettingsPage /> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  return render(<RouterProvider router={router} />);
}

describe("app shell", () => {
  it("renders the brand and a primary sidebar nav", () => {
    renderAt("/wanted");
    const nav = screen.getByRole("navigation", { name: /primary/i });
    expect(nav).toBeInTheDocument();
    expect(within(nav).getByText("Collapsarr")).toBeInTheDocument();
  });

  it("links to Wanted, Activity, and Settings", () => {
    renderAt("/wanted");
    const nav = screen.getByRole("navigation", { name: /primary/i });
    expect(within(nav).getByRole("link", { name: /wanted/i })).toHaveAttribute(
      "href",
      "/wanted",
    );
    expect(within(nav).getByRole("link", { name: /activity/i })).toHaveAttribute(
      "href",
      "/activity",
    );
    expect(within(nav).getByRole("link", { name: /settings/i })).toHaveAttribute(
      "href",
      "/settings",
    );
  });

  it("marks the current route active", () => {
    renderAt("/activity");
    const nav = screen.getByRole("navigation", { name: /primary/i });
    const active = within(nav).getByRole("link", { name: /activity/i });
    expect(active.className).toContain("sidebar__link--active");
  });

  it("renders each view's heading", () => {
    renderAt("/wanted");
    expect(screen.getByRole("heading", { name: "Wanted" })).toBeInTheDocument();

    renderAt("/activity");
    expect(screen.getByRole("heading", { name: "Activity" })).toBeInTheDocument();

    renderAt("/settings");
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
  });
});
