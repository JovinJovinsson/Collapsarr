import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../components/AppShell";
import { ActivityPage } from "../pages/ActivityPage";
import { SettingsPage } from "../pages/SettingsPage";
import { WantedPage } from "../pages/WantedPage";

// WantedPage (COL-31) fetches `/api/wanted` on mount; stub it so these
// shell-level tests don't hit the network or trigger act() warnings from an
// unmocked fetch. Response contents are irrelevant here -- WantedPage's own
// behaviour is covered by src/test/wantedPage.test.tsx.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve([]),
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

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
  it("renders the brand and a primary sidebar nav", async () => {
    renderAt("/wanted");
    const nav = await screen.findByRole("navigation", { name: /primary/i });
    expect(nav).toBeInTheDocument();
    expect(within(nav).getByText("Collapsarr")).toBeInTheDocument();
  });

  it("links to Wanted, Activity, and Settings", async () => {
    renderAt("/wanted");
    const nav = await screen.findByRole("navigation", { name: /primary/i });
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

  it("marks the current route active", async () => {
    renderAt("/activity");
    const nav = await screen.findByRole("navigation", { name: /primary/i });
    const active = within(nav).getByRole("link", { name: /activity/i });
    expect(active.className).toContain("sidebar__link--active");
  });

  it("renders each view's heading", async () => {
    renderAt("/wanted");
    expect(await screen.findByRole("heading", { name: "Wanted" })).toBeInTheDocument();

    renderAt("/activity");
    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();

    renderAt("/settings");
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
  });
});
