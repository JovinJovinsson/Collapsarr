import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StatusPage } from "../pages/StatusPage";
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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("StatusPage", () => {
  it("renders the About panel with data from GET /api/system/info", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(systemInfo));
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
      .mockResolvedValueOnce(jsonResponse({ ...systemInfo, ffmpeg_version: null }));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText("Not found")).toBeInTheDocument());
  });

  it("shows an error state when the initial load fails", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);

    render(<StatusPage />);

    await waitFor(() => expect(screen.getByText(/Couldn't load system info/)).toBeInTheDocument());
  });
});
