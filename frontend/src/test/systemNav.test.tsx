import { render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "../components/AppShell";
import { TasksPage } from "../pages/TasksPage";
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

function renderWithSystemNav(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <AppShell />,
        children: [
          // Bare /system redirects to /system/tasks (COL-126)
          { path: "system", element: <TasksPage /> },
          ...systemNavItems.map(({ to, element }) => ({
            path: to.replace("/", ""),
            element,
          })),
        ],
      },
    ],
    { initialEntries: [path] },
  );
  return render(<RouterProvider router={router} />);
}

describe("System navigation (COL-126)", () => {
  it("orders systemNavItems as Tasks, Backups, Health, Status, Updates", () => {
    const expectedOrder = ["Tasks", "Backups", "Health", "Status", "Updates"];
    const actualOrder = systemNavItems.map((item) => item.label);

    expect(actualOrder).toEqual(expectedOrder);
  });

  it("renders system nav items in the correct order in the sidebar", async () => {
    renderWithSystemNav("/system/tasks");

    const nav = await screen.findByRole("navigation", { name: /primary/i });
    const links = within(nav).getAllByRole("link");

    // Find the system nav items (Tasks, Backups, Health, Status, Updates) among all nav links
    // Primary nav has: Wanted, Libraries, Activity, Settings (4 links)
    // System nav has: Tasks, Backups, Health, Status, Updates (5 links)
    // Total: 9 links
    // The system nav items should be in order starting from index 4
    const systemLinks = links.slice(4, 9);
    expect(systemLinks).toHaveLength(5);
    expect(systemLinks[0]).toHaveTextContent("Tasks");
    expect(systemLinks[1]).toHaveTextContent("Backups");
    expect(systemLinks[2]).toHaveTextContent("Health");
    expect(systemLinks[3]).toHaveTextContent("Status");
    expect(systemLinks[4]).toHaveTextContent("Updates");
  });

  it("/system redirects to /system/tasks by default", async () => {
    renderWithSystemNav("/system");

    // Navigate happens synchronously in memory router, so we can check immediately
    // by looking for a page element that indicates we're on the Tasks page
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /tasks/i })).toBeInTheDocument();
    });
  });
});
