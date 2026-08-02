import { render, screen, waitFor, within } from "@testing-library/react";
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
});
