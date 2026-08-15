import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StatusPage } from "../pages/StatusPage";
import type { HealthCheckState } from "../types/health";
import type { SystemInfo } from "../types/system";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const systemInfo: SystemInfo = {
  app_version: "1.2.3",
  install_method: "docker",
  python_version: "3.12.4",
  ffmpeg_version: "6.1.1",
  os: "Linux-6.1.0-x86_64",
  db_engine: "sqlite",
  db_schema_revision: "abcdef123456",
  data_dir: "/config",
  database_path: "/config/collapsarr.db",
  uptime_seconds: 3725,
  timezone: "UTC",
  disk: { free_bytes: 1_800_000_000, total_bytes: 2_000_000_000 },
};

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

describe("StatusPage", () => {
  it("renders the About panel with data from GET /api/system/info", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(systemInfo))
      .mockResolvedValueOnce(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());
    expect(screen.getByText("docker")).toBeInTheDocument();
    expect(screen.getByText("3.12.4")).toBeInTheDocument();
    expect(screen.getByText("6.1.1")).toBeInTheDocument();
    expect(screen.getByText("Linux-6.1.0-x86_64")).toBeInTheDocument();
    expect(screen.getByText("sqlite")).toBeInTheDocument();
    expect(screen.getByText("abcdef123456")).toBeInTheDocument();
    expect(screen.getByText("/config")).toBeInTheDocument();
    expect(screen.getByText("/config/collapsarr.db")).toBeInTheDocument();
    // 3725s = 1h 2m.
    expect(screen.getByText("1h 2m")).toBeInTheDocument();
    expect(screen.getByText("UTC")).toBeInTheDocument();
    expect(screen.getByText(/1\.7 GB free of 1\.9 GB/)).toBeInTheDocument();

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/system/info", expect.anything());
  });

  it("renders 'Not found' when FFmpeg is unavailable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ ...systemInfo, ffmpeg_version: null }))
      .mockResolvedValueOnce(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText("Not found")).toBeInTheDocument());
  });

  it("shows an error state when the initial load fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500))
      .mockResolvedValueOnce(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText(/Couldn't load system info/)).toBeInTheDocument());
  });

  // --------------------------------------------------------------------- //
  // COL-125: Health summary
  // --------------------------------------------------------------------- //

  it("lists currently-failing, non-dismissed checks (code + message), with no dismiss controls", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(systemInfo))
      .mockResolvedValueOnce(
        jsonResponse([errorCheck, warningCheck, passingCheck, dismissedCheck])
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText("ERR-CONN-001")).toBeInTheDocument());
    expect(screen.getByText("Sonarr instance unreachable.")).toBeInTheDocument();
    expect(screen.getByText("WARN-DISK-001")).toBeInTheDocument();
    expect(screen.getByText("Disk space low.")).toBeInTheDocument();

    // Passing and dismissed checks are omitted entirely (COL-125: this is a
    // read-only glance at what's currently failing, not the full list).
    expect(screen.queryByText("ffmpeg_missing")).not.toBeInTheDocument();
    expect(screen.queryByText("ERR-DISMISSED-001")).not.toBeInTheDocument();

    // No dismiss/undismiss controls -- that stays HealthChecksPage's job.
    expect(screen.queryByRole("button", { name: /dismiss/i })).not.toBeInTheDocument();

    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/system/health-checks", expect.anything());
  });

  it("renders an 'all clear' state when nothing is failing", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(systemInfo))
      .mockResolvedValueOnce(jsonResponse([passingCheck]));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() =>
      expect(screen.getByText(/all health checks are passing/i)).toBeInTheDocument()
    );
    expect(screen.queryByText("ffmpeg_missing")).not.toBeInTheDocument();
  });

  it("renders an 'all clear' state when the health-checks list is empty", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(systemInfo))
      .mockResolvedValueOnce(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() =>
      expect(screen.getByText(/all health checks are passing/i)).toBeInTheDocument()
    );
  });

  it("shows an error message when the health-checks fetch fails, without blocking the About panel", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(systemInfo))
      .mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());
    await waitFor(() =>
      expect(screen.getByText(/Couldn't load health checks/)).toBeInTheDocument()
    );
  });

  // --------------------------------------------------------------------- //
  // COL-125: More Info links
  // --------------------------------------------------------------------- //

  it("renders the four More Info links", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(systemInfo))
      .mockResolvedValueOnce(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());

    const source = screen.getByRole("link", { name: "Source" });
    const reportIssue = screen.getByRole("link", { name: "Report an issue" });
    const apiDocs = screen.getByRole("link", { name: "API documentation" });
    const community = screen.getByRole("link", { name: "Community" });

    expect(source).toHaveAttribute("href", "https://github.com/JovinJovinsson/Collapsarr");
    expect(reportIssue).toHaveAttribute(
      "href",
      "https://github.com/JovinJovinsson/Collapsarr/issues/new"
    );
    expect(apiDocs).toHaveAttribute("href", "/docs");
    expect(community).toHaveAttribute("href", "https://www.reddit.com/r/Collapsarr/");

    for (const link of [source, reportIssue, apiDocs, community]) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    }
  });
});
