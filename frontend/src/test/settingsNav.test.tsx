import { render, screen, waitFor, within } from "@testing-library/react";
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

/**
 * COL-142 established this suite; COL-143 (Targets/Connect) and COL-144
 * (Sonarr/Radarr, plus the composed `SettingsPage`'s removal) extended it.
 * COL-146 (Phase 2) appends Scheduler as the sixth entry. Settings is now
 * fully migrated -- `/settings` is a bare redirect to `/settings/general`
 * (mirroring `/system` -> `/system/tasks`), so the `NavSection` `to`/
 * expand-base tension COL-142's review flagged (the sidebar staying
 * collapsed while the old composed page or Targets/Connect were active,
 * since `to` pointed at the narrower `/settings/general`) is gone: `to` is
 * `/settings` itself now, exactly like System's `to={SYSTEM_PATH}`, so every
 * `/settings/*` route expands the group correctly.
 */
describe("Settings navigation (COL-142, COL-143, COL-144, COL-146)", () => {
  it("orders settingsNavItems as General, Sonarr, Radarr, Targets, Connect, Scheduler", () => {
    const expectedOrder = ["General", "Sonarr", "Radarr", "Targets", "Connect", "Scheduler"];
    const actualOrder = settingsNavItems.map((item) => item.label);

    expect(actualOrder).toEqual(expectedOrder);
  });

  it("the Settings sidebar entry links to /settings", async () => {
    renderWithSettingsNav("/wanted");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    const settingsLink = within(nav).getByRole("link", { name: "Settings" });
    expect(settingsLink).toHaveAttribute("href", "/settings");
  });

  it("renders settings nav items in the correct order in the sidebar", async () => {
    renderWithSettingsNav("/settings/general");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    const links = within(nav).getAllByRole("link");

    // Scope to the Settings sub-nav links by href (against settingsNavItems'
    // own `to` values) rather than a fixed positional slice, mirroring
    // systemNav.test.tsx's equivalent assertion.
    const settingsHrefs = new Set(settingsNavItems.map((item) => item.to));
    const settingsLinks = links.filter((link) => settingsHrefs.has(link.getAttribute("href") ?? ""));
    expect(settingsLinks).toHaveLength(6);
    expect(settingsLinks[0]).toHaveTextContent("General");
    expect(settingsLinks[1]).toHaveTextContent("Sonarr");
    expect(settingsLinks[2]).toHaveTextContent("Radarr");
    expect(settingsLinks[3]).toHaveTextContent("Targets");
    expect(settingsLinks[4]).toHaveTextContent("Connect");
    expect(settingsLinks[5]).toHaveTextContent("Scheduler");
  });

  // COL-142/COL-144: Settings renders through the shared `NavSection`
  // (COL-141), which expands (sub-items visible) only while a route under
  // its `to` (now `/settings` itself, COL-144) is active -- analogous to
  // systemNav.test.tsx's collapsed/expanded assertions for System.
  it("is collapsed (no Settings sub-items) while a different section is active", async () => {
    renderWithSettingsNav("/wanted");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    expect(within(nav).getByRole("link", { name: "Settings" })).toBeInTheDocument();
    for (const item of settingsNavItems) {
      expect(within(nav).queryByRole("link", { name: item.label })).not.toBeInTheDocument();
    }
  });

  it.each(settingsNavItems.map((item) => [item.label, item.to]))(
    "expands to list every Settings sub-item under %s (%s)",
    async (_label, to) => {
      renderWithSettingsNav(to);

      const nav = await screen.findByRole("navigation", { name: /primary/i });
      for (const item of settingsNavItems) {
        expect(await within(nav).findByRole("link", { name: item.label })).toBeInTheDocument();
      }
    },
  );

  // COL-144: the old composed `/settings` page (Instances/Targets/Connect)
  // and its route are gone -- a bare `/settings` now redirects to
  // `/settings/general`, mirroring the bare `/system` -> `/system/tasks`
  // redirect (systemNav.test.tsx).
  it("/settings redirects to /settings/general by default", async () => {
    renderWithSettingsNav("/settings");

    // `level: 1` disambiguates the page's own `<h1>General</h1>` from
    // `GeneralSection`'s nested `<h2>General</h2>` (same page-h1-plus-
    // section-h2 nesting every Settings sub-page has -- see
    // SettingsGeneralPage's doc comment).
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /general/i, level: 1 })).toBeInTheDocument();
    });
  });

  it("is expanded (not collapsed) on the bare /settings redirect, since it lands under /settings/*", async () => {
    renderWithSettingsNav("/settings");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    for (const item of settingsNavItems) {
      expect(await within(nav).findByRole("link", { name: item.label })).toBeInTheDocument();
    }
  });
});
