import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HealthChecksPage } from "../pages/HealthChecksPage";
import type { HealthCheckState } from "../types/health";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const errorCheck: HealthCheckState = {
  id: 1,
  code: "ERR-CONN-001",
  category: "connectivity",
  status: "failing",
  severity: "error",
  message: "Sonarr instance unreachable.",
  instance_id: 3,
  first_failed_at: "2026-08-01T10:00:00Z",
  last_checked_at: "2026-08-02T10:00:00Z",
  dismissed_at: null,
};

const warningCheck: HealthCheckState = {
  id: 2,
  code: "WARN-DISK-001",
  category: "disk",
  status: "failing",
  severity: "warning",
  message: "Disk space low.",
  instance_id: null,
  first_failed_at: "2026-08-01T09:00:00Z",
  last_checked_at: "2026-08-02T10:00:00Z",
  dismissed_at: null,
};

const passingCheck: HealthCheckState = {
  id: 3,
  code: "ffmpeg_missing",
  category: "ffmpeg",
  status: "passing",
  severity: "error",
  message: "FFmpeg found at '/usr/bin/ffmpeg'.",
  instance_id: null,
  first_failed_at: null,
  last_checked_at: "2026-08-02T10:00:00Z",
  dismissed_at: null,
};

const dismissedCheck: HealthCheckState = {
  id: 4,
  code: "ERR-DISMISSED-001",
  category: "test",
  status: "failing",
  severity: "error",
  message: "Still failing, but dismissed.",
  instance_id: null,
  first_failed_at: "2026-08-01T10:00:00Z",
  last_checked_at: "2026-08-02T10:00:00Z",
  dismissed_at: "2026-08-02T09:00:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("HealthChecksPage", () => {
  it("shows a loading state, then every check, mixed severities included", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse([errorCheck, warningCheck, passingCheck]))
    );
    render(<HealthChecksPage />);

    expect(screen.getByText(/loading health checks/i)).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("ERR-CONN-001")).toBeInTheDocument());

    expect(screen.getByText("WARN-DISK-001")).toBeInTheDocument();
    expect(screen.getByText("ffmpeg_missing")).toBeInTheDocument();

    // Generic over any number/kind of check -- all three distinct codes render,
    // not hardcoded to FFmpeg.
    const rows = screen.getAllByRole("row");
    // header row + 3 data rows
    expect(rows).toHaveLength(4);
  });

  it("visually distinguishes warning- from error-severity rows for multiple simultaneous checks", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([errorCheck, warningCheck])));
    render(<HealthChecksPage />);

    const errorRow = (await screen.findByText("ERR-CONN-001")).closest("tr");
    const warningRow = screen.getByText("WARN-DISK-001").closest("tr");
    expect(errorRow).not.toBeNull();
    expect(warningRow).not.toBeNull();

    const errorSeverity = within(errorRow as HTMLElement).getByText("Error");
    const warningSeverity = within(warningRow as HTMLElement).getByText("Warning");

    expect(errorSeverity).toHaveClass("health-table__severity--error");
    expect(warningSeverity).toHaveClass("health-table__severity--warning");
    expect(errorSeverity.className).not.toBe(warningSeverity.className);
  });

  it("hides severity badges for passing checks, showing only status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([errorCheck, passingCheck])));
    render(<HealthChecksPage />);

    const failingRow = (await screen.findByText("ERR-CONN-001")).closest("tr") as HTMLElement;
    const passingRow = screen.getByText("ffmpeg_missing").closest("tr") as HTMLElement;

    // Failing check shows severity badge
    expect(within(failingRow).getByText("Error")).toBeInTheDocument();
    expect(within(failingRow).getByText("Error")).toHaveClass("health-table__severity--error");

    // Passing check does not show severity badge (even though it has severity="error")
    expect(within(passingRow).queryByText("Error")).not.toBeInTheDocument();
    expect(within(passingRow).getByText("Passing")).toBeInTheDocument();
  });

  it("shows the per-instance id and both passing/failing statuses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([errorCheck, passingCheck])));
    render(<HealthChecksPage />);

    await waitFor(() => expect(screen.getByText("ERR-CONN-001")).toBeInTheDocument());
    expect(screen.getByText(/instance 3/i)).toBeInTheDocument();
    expect(screen.getByText("Failing")).toBeInTheDocument();
    expect(screen.getByText("Passing")).toBeInTheDocument();
  });

  it("shows an empty state when no checks have run yet", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([])));
    render(<HealthChecksPage />);

    await waitFor(() => expect(screen.getByText(/no health checks have run yet/i)).toBeInTheDocument());
  });

  it("shows an error state when the fetch fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<HealthChecksPage />);

    await waitFor(() =>
      expect(screen.getByText(/couldn.t load health checks: network down/i)).toBeInTheDocument()
    );
  });

  // --------------------------------------------------------------------- //
  // COL-82: dismiss / undismiss
  // --------------------------------------------------------------------- //

  it("offers Dismiss only for a currently-failing, not-yet-dismissed check", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse([errorCheck, passingCheck, dismissedCheck]))
    );
    render(<HealthChecksPage />);

    const failingRow = (await screen.findByText("ERR-CONN-001")).closest("tr") as HTMLElement;
    const passingRow = screen.getByText("ffmpeg_missing").closest("tr") as HTMLElement;
    const dismissedRow = screen.getByText("ERR-DISMISSED-001").closest("tr") as HTMLElement;

    expect(within(failingRow).getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(within(passingRow).queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    expect(within(passingRow).queryByRole("button", { name: "Undismiss" })).not.toBeInTheDocument();
    expect(within(dismissedRow).getByRole("button", { name: "Undismiss" })).toBeInTheDocument();
    expect(within(dismissedRow).queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    expect(within(dismissedRow).getByText("Dismissed")).toBeInTheDocument();
  });

  it("dismisses a failing check and reloads the list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([errorCheck]))
      .mockResolvedValueOnce(jsonResponse({ ...errorCheck, dismissed_at: "2026-08-02T12:00:00Z" }))
      .mockResolvedValueOnce(jsonResponse([{ ...errorCheck, dismissed_at: "2026-08-02T12:00:00Z" }]));
    vi.stubGlobal("fetch", fetchMock);
    render(<HealthChecksPage />);

    const row = (await screen.findByText("ERR-CONN-001")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(within(row).getByText("Dismissed")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/system/health-checks/1/dismiss",
      expect.objectContaining({ method: "POST" })
    );
    // Reloaded the list after a successful dismiss.
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("undismisses a check and reloads the list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([dismissedCheck]))
      .mockResolvedValueOnce(jsonResponse({ ...dismissedCheck, dismissed_at: null }))
      .mockResolvedValueOnce(jsonResponse([{ ...dismissedCheck, dismissed_at: null }]));
    vi.stubGlobal("fetch", fetchMock);
    render(<HealthChecksPage />);

    const row = (await screen.findByText("ERR-DISMISSED-001")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Undismiss" }));

    await waitFor(() =>
      expect(within(row).getByRole("button", { name: "Dismiss" })).toBeInTheDocument()
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/system/health-checks/4/undismiss",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("shows an error banner when a dismiss fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([errorCheck]))
      .mockResolvedValueOnce(jsonResponse({ detail: "not currently failing" }, 409));
    vi.stubGlobal("fetch", fetchMock);
    render(<HealthChecksPage />);

    const row = (await screen.findByText("ERR-CONN-001")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Dismiss" }));

    await waitFor(() =>
      expect(screen.getByText(/not currently failing/i)).toBeInTheDocument()
    );
  });

  // --------------------------------------------------------------------- //
  // COL-83: manual recheck
  // --------------------------------------------------------------------- //

  it("shows a Recheck now button that reflects updated results without a full page reload", async () => {
    const recheckedCheck: HealthCheckState = {
      ...errorCheck,
      status: "passing",
      message: "Sonarr instance reachable.",
      first_failed_at: null,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([errorCheck]))
      .mockResolvedValueOnce(jsonResponse([recheckedCheck]));
    vi.stubGlobal("fetch", fetchMock);
    render(<HealthChecksPage />);

    await waitFor(() => expect(screen.getByText("ERR-CONN-001")).toBeInTheDocument());
    const row = screen.getByText("ERR-CONN-001").closest("tr") as HTMLElement;
    expect(within(row).getByText("Failing")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Recheck now" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/system/health-checks/recheck",
      expect.objectContaining({ method: "POST" })
    );
    await waitFor(() =>
      expect(within(row).getByText("Passing")).toBeInTheDocument()
    );
    // Exactly the recheck round-trip -- no extra GET afterwards, since the
    // recheck response already carries the full, fresh state.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows an error banner when a recheck fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([errorCheck]))
      .mockResolvedValueOnce(jsonResponse({ detail: "scheduler unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    render(<HealthChecksPage />);

    await waitFor(() => expect(screen.getByText("ERR-CONN-001")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Recheck now" }));

    await waitFor(() => expect(screen.getByText(/scheduler unavailable/i)).toBeInTheDocument());
    // The stale list is left in place -- a failed recheck doesn't wipe it out.
    expect(screen.getByText("ERR-CONN-001")).toBeInTheDocument();
  });
});
