import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HealthBanner } from "../components/HealthBanner";
import { HealthProvider } from "../components/HealthProvider";
import type { HealthStatus } from "../types/health";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
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
});
