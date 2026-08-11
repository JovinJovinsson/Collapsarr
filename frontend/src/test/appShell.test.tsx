import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "../components/AppShell";
import { ActivityPage } from "../pages/ActivityPage";
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

// Settings (COL-144) is now an expandable NavSection group rather than a
// single destination page (like System), so this hand-rolled shell fixture
// no longer mounts a "settings" route -- settingsNav.test.tsx exercises the
// real production route tree (routes/router.tsx) for Settings' link/expand/
// redirect behavior, and settingsGeneral.test.tsx etc. cover its sub-pages'
// content.
function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <AppShell />,
        children: [
          { path: "wanted", element: <WantedPage /> },
          { path: "activity", element: <ActivityPage /> },
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
    // Settings' sidebar entry is an expandable NavSection group (COL-141),
    // like System -- its default link is the group's own base path
    // (COL-144, once every sub-page had migrated off the old composed
    // /settings route). See settingsNav.test.tsx for the fuller
    // expand/collapse/redirect coverage.
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

    // Settings no longer has a single composed page/heading to render here
    // (COL-144) -- its sub-pages' headings are covered by their own test
    // files (settingsGeneral.test.tsx, settingsSonarr.test.tsx, etc.).
  });
});
