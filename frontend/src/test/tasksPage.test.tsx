import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TasksPage } from "../pages/TasksPage";
import type { ScheduledTask } from "../types/tasks";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const libraryScan: ScheduledTask = {
  name: "Library scan",
  interval_label: "Every 6 hours",
  next_run_at: "2026-08-09T12:00:00Z",
  last_run_at: "2026-08-09T06:00:00Z",
  scheduler_enabled: true,
};

const healthChecks: ScheduledTask = {
  name: "Health checks",
  interval_label: "Every 5 minutes",
  next_run_at: null,
  last_run_at: "2026-08-09T09:55:00Z",
  scheduler_enabled: false,
};

const backups: ScheduledTask = {
  name: "Backups",
  interval_label: "Every 7 days",
  next_run_at: null,
  last_run_at: null,
  scheduler_enabled: false,
};

const updateCheck: ScheduledTask = {
  name: "Update check",
  interval_label: "Every 24 hours",
  next_run_at: "2026-08-10T09:00:00Z",
  last_run_at: "2026-08-09T09:00:00Z",
  scheduler_enabled: true,
};

const plexSync: ScheduledTask = {
  name: "Plex Sync",
  interval_label: "Every 1 week",
  next_run_at: "2026-08-16T09:00:00Z",
  last_run_at: "2026-08-09T09:00:00Z",
  scheduler_enabled: true,
};

const allTasks: ScheduledTask[] = [libraryScan, healthChecks, backups, updateCheck, plexSync];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("TasksPage", () => {
  it("renders every Scheduled Task with its interval and next execution", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(allTasks));
    vi.stubGlobal("fetch", fetchMock);

    render(<TasksPage />);

    await waitFor(() => expect(screen.getByText("Library scan")).toBeInTheDocument());
    expect(screen.getByText("Health checks")).toBeInTheDocument();
    expect(screen.getByText("Backups")).toBeInTheDocument();
    expect(screen.getByText("Update check")).toBeInTheDocument();
    expect(screen.getByText("Plex Sync")).toBeInTheDocument();
    expect(screen.getByText("Every 6 hours")).toBeInTheDocument();
    expect(screen.getByText("Every 1 week")).toBeInTheDocument();

    // A null next_run_at (scheduler disabled / never run) renders as an em dash.
    const backupsRow = screen.getByText("Backups").closest("tr") as HTMLElement;
    expect(within(backupsRow).getByText("—")).toBeInTheDocument();

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/system/tasks", expect.anything());
  });

  it("shows an error state when the initial load fails", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);

    render(<TasksPage />);

    await waitFor(() => expect(screen.getByText(/Couldn't load tasks/)).toBeInTheDocument());
  });

  it("runs a task via its existing trigger endpoint and refetches the list", async () => {
    const refreshedLibraryScan: ScheduledTask = {
      ...libraryScan,
      last_run_at: "2026-08-09T12:00:00Z",
      next_run_at: "2026-08-09T18:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(allTasks)) // initial GET on mount
      .mockResolvedValueOnce(jsonResponse({ enqueued: [] }, 202)) // POST /api/jobs/scan
      .mockResolvedValueOnce(
        jsonResponse([refreshedLibraryScan, healthChecks, backups, updateCheck])
      ); // refetch GET
    vi.stubGlobal("fetch", fetchMock);

    render(<TasksPage />);

    await waitFor(() => expect(screen.getByText("Library scan")).toBeInTheDocument());
    const row = screen.getByText("Library scan").closest("tr") as HTMLElement;

    fireEvent.click(within(row).getByRole("button", { name: "Run now" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/jobs/scan",
      expect.objectContaining({ method: "POST" })
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/system/tasks", expect.anything());
  });

  it("runs the Plex Sync task via POST /api/plex/sync and refetches the list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(allTasks)) // initial GET on mount
      .mockResolvedValueOnce(jsonResponse({ items: 12 }, 202)) // POST /api/plex/sync
      .mockResolvedValueOnce(jsonResponse(allTasks)); // refetch GET
    vi.stubGlobal("fetch", fetchMock);

    render(<TasksPage />);

    await waitFor(() => expect(screen.getByText("Plex Sync")).toBeInTheDocument());
    const row = screen.getByText("Plex Sync").closest("tr") as HTMLElement;

    fireEvent.click(within(row).getByRole("button", { name: "Run now" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/plex/sync",
      expect.objectContaining({ method: "POST" })
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/system/tasks", expect.anything());
  });

  it("surfaces an error if a run fails, without discarding the current list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(allTasks)) // initial GET
      .mockResolvedValueOnce(jsonResponse({ detail: "scheduler unavailable" }, 503)); // POST failure
    vi.stubGlobal("fetch", fetchMock);

    render(<TasksPage />);

    await waitFor(() => expect(screen.getByText("Library scan")).toBeInTheDocument());
    const row = screen.getByText("Library scan").closest("tr") as HTMLElement;

    fireEvent.click(within(row).getByRole("button", { name: "Run now" }));

    await waitFor(() => expect(screen.getByText("scheduler unavailable")).toBeInTheDocument());
    // No refetch happened after the failed POST -- the list is unchanged.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Library scan")).toBeInTheDocument();
  });
});
