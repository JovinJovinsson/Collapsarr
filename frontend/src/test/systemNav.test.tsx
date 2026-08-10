import { render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { routes } from "../routes/router";
import { systemNavItems } from "../routes/nav";

// Mock fetch for pages that fetch on mount
vi.stubGlobal(
  "fetch",
  vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve([]),
  }),
);

// Rendering the real router (below) means the bare `/system` route's
// `<Navigate>` actually runs a client-side navigation through react-router's
// data router, which builds an internal `Request` for every transition (even
// with no loaders). Vitest's jsdom environment substitutes its own
// AbortController/AbortSignal implementation for the global one so DOM code
// gets spec-accurate behaviour, but Node's built-in `Request` only accepts a
// signal it recognises as *its own* AbortSignal, so that internal `new
// Request(url, { signal })` throws under jsdom. Stub `Request` with a
// minimal, non-validating constructor here so the real navigation can
// actually run in this test environment; nothing in these tests depends on
// `Request`'s real fetch semantics.
vi.stubGlobal(
  "Request",
  class TestRequest {
    constructor(input: unknown, init: RequestInit = {}) {
      Object.assign(this, { url: String(input), ...init });
    }
  },
);

// Uses the real production `routes` (from routes/router.tsx) rather than a
// hand-rolled fixture, so this exercises the actual `<Navigate>` the app
// ships for the bare /system route (COL-126 code review) -- if that redirect
// target ever changes or is removed, these tests fail for real.
function renderWithSystemNav(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(<RouterProvider router={router} />);
}

describe("System navigation (COL-126, COL-131)", () => {
  it("orders systemNavItems as Tasks, Backups, Health, Status, Updates, Logs", () => {
    const expectedOrder = ["Tasks", "Backups", "Health", "Status", "Updates", "Logs"];
    const actualOrder = systemNavItems.map((item) => item.label);

    expect(actualOrder).toEqual(expectedOrder);
  });

  it("renders system nav items in the correct order in the sidebar", async () => {
    renderWithSystemNav("/system/tasks");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    const links = within(nav).getAllByRole("link");

    // Scope to the System nav links by href (against systemNavItems' own `to`
    // values) rather than a fixed positional slice, so this doesn't assume
    // exactly how many links precede System -- e.g. LibraryNavSection
    // rendering extra sub-links wouldn't shift a hardcoded offset out from
    // under this assertion (COL-126 code review).
    const systemHrefs = new Set(systemNavItems.map((item) => item.to));
    const systemLinks = links.filter((link) => systemHrefs.has(link.getAttribute("href") ?? ""));
    expect(systemLinks).toHaveLength(6);
    expect(systemLinks[0]).toHaveTextContent("Tasks");
    expect(systemLinks[1]).toHaveTextContent("Backups");
    expect(systemLinks[2]).toHaveTextContent("Health");
    expect(systemLinks[3]).toHaveTextContent("Status");
    expect(systemLinks[4]).toHaveTextContent("Updates");
    expect(systemLinks[5]).toHaveTextContent("Logs");
  });

  it("/system redirects to /system/tasks by default", async () => {
    renderWithSystemNav("/system");

    // Navigate happens synchronously in memory router, so we can check immediately
    // by looking for a page element that indicates we're on the Tasks page
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /tasks/i })).toBeInTheDocument();
    });
  });

  // COL-141: System renders through the shared `NavSection`, which expands
  // (sub-items visible) only while a route under its base path (/system) is
  // active -- analogous to librariesNav.test.tsx's collapsed/expanded
  // assertions for `LibraryNavSection`'s fixed-list counterpart.
  it("is collapsed (no System sub-items) while a different section is active", async () => {
    renderWithSystemNav("/wanted");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    expect(within(nav).getByRole("link", { name: "System" })).toBeInTheDocument();
    for (const item of systemNavItems) {
      expect(within(nav).queryByRole("link", { name: item.label })).not.toBeInTheDocument();
    }
  });

  it("expands to list every System sub-item under any /system/* route", async () => {
    renderWithSystemNav("/system/health");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    for (const item of systemNavItems) {
      expect(await within(nav).findByRole("link", { name: item.label })).toBeInTheDocument();
    }
  });
});
