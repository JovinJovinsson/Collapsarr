import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HealthBanner } from "../components/HealthBanner";
import { HealthProvider } from "../components/HealthProvider";
import type { HealthStatus } from "../types/health";
import type { InstallMethod, SystemInfo } from "../types/system";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const SYSTEM_INFO_BASE: Omit<SystemInfo, "install_method"> = {
  app_version: "0.1.0",
  python_version: "3.12.0",
  ffmpeg_version: null,
  os: "Linux",
  db_engine: "sqlite",
  db_schema_revision: "abc123",
  data_dir: "/data",
  database_path: "/data/collapsarr.db",
  uptime_seconds: 10,
  timezone: "UTC",
  disk: { free_bytes: 1000, total_bytes: 2000 },
};

function systemInfo(install_method: InstallMethod): SystemInfo {
  return { ...SYSTEM_INFO_BASE, install_method };
}

/**
 * Routes a stubbed `fetch` by request URL: `/health` gets `health`,
 * `/api/system/info` gets `installMethod`, and `/api/system/ffmpeg/download`
 * is handled by `downloadHandler` (COL-222's opt-in action) -- lets a single
 * test drive all three endpoints `HealthBanner` now touches.
 */
function fetchRouter(options: {
  health: HealthStatus;
  installMethod?: InstallMethod;
  downloadHandler?: () => { ok: boolean; status: number; json: () => Promise<unknown> };
}) {
  return vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/system/ffmpeg/download")) {
      if (options.downloadHandler) return Promise.resolve(options.downloadHandler());
      return Promise.resolve(jsonResponse({ ffmpeg_path: "/data/ffmpeg/ffmpeg" }));
    }
    if (url.includes("/api/system/info")) {
      return Promise.resolve(
        options.installMethod
          ? jsonResponse(systemInfo(options.installMethod))
          : jsonResponse({ detail: "not found" }, 404),
      );
    }
    return Promise.resolve(jsonResponse(options.health));
  });
}

/**
 * `HealthBanner` reads the shared `GET /health` state via `useHealth()`
 * (COL-124 code review), so it must be rendered beneath `HealthProvider`.
 */
function renderBanner() {
  return render(
    <HealthProvider>
      <HealthBanner />
    </HealthProvider>,
  );
}

const okHealth: HealthStatus = { status: "ok", version: "0.1.0", warnings: [] };
const degradedHealth: HealthStatus = {
  status: "degraded",
  version: "0.1.0",
  warnings: [
    {
      code: "ffmpeg_missing",
      message: "FFmpeg executable 'ffmpeg' was not found on PATH.",
      severity: "error",
    },
  ],
};

const mixedSeverityHealth: HealthStatus = {
  status: "degraded",
  version: "0.1.0",
  warnings: [
    { code: "ffmpeg_missing", message: "FFmpeg executable 'ffmpeg' was not found on PATH.", severity: "error" },
    { code: "WARN-DISK-001", message: "Disk space low.", severity: "warning" },
  ],
};

describe("HealthBanner", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders nothing when the app reports ok", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(okHealth));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderBanner();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/health", expect.anything()));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a warning banner when the app reports degraded (FFmpeg missing)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(degradedHealth)));
    renderBanner();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/ffmpeg executable 'ffmpeg' was not found on path/i);
  });

  it("visually distinguishes warning- from error-severity entries when several checks fail at once (COL-76)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(mixedSeverityHealth)));
    renderBanner();

    const alert = await screen.findByRole("alert");
    const messages = alert.querySelectorAll(".health-banner__message");
    expect(messages).toHaveLength(2);

    const errorEntry = Array.from(messages).find((el) => el.textContent?.includes("FFmpeg executable"));
    const warningEntry = Array.from(messages).find((el) => el.textContent?.includes("Disk space low"));

    expect(errorEntry).toBeDefined();
    expect(warningEntry).toBeDefined();
    expect(errorEntry).toHaveClass("health-banner__message--error");
    expect(warningEntry).toHaveClass("health-banner__message--warning");
    // Distinct styling, not just distinct text -- the two entries must not
    // share the same severity modifier class.
    expect(errorEntry?.className).not.toBe(warningEntry?.className);
  });

  it("renders nothing when the health fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderBanner();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/health", expect.anything()));
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  // --------------------------------------------------------------------- //
  // COL-222: opt-in "Download FFmpeg" action on the ffmpeg_missing row
  // --------------------------------------------------------------------- //

  it("shows the Download FFmpeg action for a native install", async () => {
    vi.stubGlobal("fetch", fetchRouter({ health: degradedHealth, installMethod: "native" }));
    renderBanner();

    await screen.findByRole("alert");
    expect(await screen.findByRole("button", { name: /download ffmpeg/i })).toBeInTheDocument();
  });

  it("shows the Download FFmpeg action for a pipx install", async () => {
    vi.stubGlobal("fetch", fetchRouter({ health: degradedHealth, installMethod: "pipx" }));
    renderBanner();

    await screen.findByRole("alert");
    expect(await screen.findByRole("button", { name: /download ffmpeg/i })).toBeInTheDocument();
  });

  it("hides the Download FFmpeg action for a Docker install", async () => {
    vi.stubGlobal("fetch", fetchRouter({ health: degradedHealth, installMethod: "docker" }));
    renderBanner();

    await screen.findByRole("alert");
    // Give the (never-called-for-real, but still-awaited) install-method
    // fetch a tick to resolve before asserting its absence.
    await waitFor(() => expect(screen.queryByRole("button", { name: /download ffmpeg/i })).not.toBeInTheDocument());
  });

  it("hides the Download FFmpeg action while install_method is still unknown (fails closed)", async () => {
    vi.stubGlobal("fetch", fetchRouter({ health: degradedHealth }));
    renderBanner();

    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: /download ffmpeg/i })).not.toBeInTheDocument();
  });

  it("does not show the action for a check other than ffmpeg_missing", async () => {
    const diskOnlyHealth: HealthStatus = {
      status: "degraded",
      version: "0.1.0",
      warnings: [{ code: "WARN-DISK-001", message: "Disk space low.", severity: "warning" }],
    };
    vi.stubGlobal("fetch", fetchRouter({ health: diskOnlyHealth, installMethod: "native" }));
    renderBanner();

    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: /download ffmpeg/i })).not.toBeInTheDocument();
  });

  it("clicking Download FFmpeg triggers the download and clears the warning once healthy", async () => {
    let healthCallCount = 0;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/system/ffmpeg/download")) {
        return Promise.resolve(jsonResponse({ ffmpeg_path: "/data/ffmpeg/ffmpeg" }));
      }
      if (url.includes("/api/system/info")) {
        return Promise.resolve(jsonResponse(systemInfo("native")));
      }
      healthCallCount += 1;
      // First tick (mount) is degraded; the refresh triggered after a
      // successful download reports healthy -- proving the banner clears
      // without a page reload (COL-222's HealthProvider.refresh()).
      return Promise.resolve(jsonResponse(healthCallCount === 1 ? degradedHealth : okHealth));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderBanner();

    const button = await screen.findByRole("button", { name: /download ffmpeg/i });
    fireEvent.click(button);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/system/ffmpeg/download", expect.objectContaining({ method: "POST" })),
    );
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("shows a clear, retryable error when the download fails", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRouter({
        health: degradedHealth,
        installMethod: "native",
        downloadHandler: () => jsonResponse({ detail: "SHA-256 mismatch for the fetched archive." }, 502),
      }),
    );
    renderBanner();

    const button = await screen.findByRole("button", { name: /download ffmpeg/i });
    fireEvent.click(button);

    expect(await screen.findByText(/sha-256 mismatch/i)).toBeInTheDocument();
    // The action is retryable -- still present and enabled after the failure.
    const retryButton = await screen.findByRole("button", { name: /download ffmpeg/i });
    expect(retryButton).not.toBeDisabled();
  });
});
