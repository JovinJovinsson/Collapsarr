import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { settingsNavItems } from "../routes/nav";
import { routes } from "../routes/router";

// Mock fetch for pages that fetch on mount (Settings' General page among them).
vi.stubGlobal(
  "fetch",
  vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve([]),
  }),
);

// See systemNav.test.tsx for why `Request` needs a jsdom-safe stub: rendering
// the real router's redirects/navigations builds an internal `Request` even
// with no loaders, and Node's built-in `Request` rejects jsdom's substitute
// AbortSignal.
vi.stubGlobal(
  "Request",
  class TestRequest {
    constructor(input: unknown, init: RequestInit = {}) {
      Object.assign(this, { url: String(input), ...init });
    }
  },
);

// Uses the real production `routes` (from routes/router.tsx), same as
// systemNav.test.tsx, so this exercises the actual sidebar wiring the app
// ships rather than a hand-rolled fixture.
function renderWithSettingsNav(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(<RouterProvider router={router} />);
}

describe("Settings navigation (COL-142)", () => {
  it("the Settings sidebar entry links to /settings/general", async () => {
    renderWithSettingsNav("/wanted");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    const settingsLink = within(nav).getByRole("link", { name: "Settings" });
    expect(settingsLink).toHaveAttribute("href", "/settings/general");
  });

  // COL-142: Settings renders through the shared `NavSection` (COL-141),
  // which expands (sub-items visible) only while a route under its `to`
  // (/settings/general) is active -- analogous to systemNav.test.tsx's
  // collapsed/expanded assertions for System.
  it("is collapsed (no Settings sub-items) while a different section is active", async () => {
    renderWithSettingsNav("/wanted");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    expect(within(nav).getByRole("link", { name: "Settings" })).toBeInTheDocument();
    for (const item of settingsNavItems) {
      expect(within(nav).queryByRole("link", { name: item.label })).not.toBeInTheDocument();
    }
  });

  it("expands to list every Settings sub-item under /settings/general", async () => {
    renderWithSettingsNav("/settings/general");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    for (const item of settingsNavItems) {
      expect(await within(nav).findByRole("link", { name: item.label })).toBeInTheDocument();
    }
  });

  // The old composed /settings page (Instances/Targets/Connect, COL-142's
  // sequencing note) isn't under /settings/general, so it stays collapsed
  // too, same as any other non-Settings route -- no redirect ties the two
  // together yet (that ships with COL-144, the last migration).
  it("is collapsed while the old composed /settings page is active", async () => {
    renderWithSettingsNav("/settings");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    for (const item of settingsNavItems) {
      expect(within(nav).queryByRole("link", { name: item.label })).not.toBeInTheDocument();
    }
  });
});
