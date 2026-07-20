import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HealthBanner } from "../components/HealthBanner";
import type { HealthStatus } from "../types/health";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const okHealth: HealthStatus = { status: "ok", version: "0.1.0", warnings: [] };
const degradedHealth: HealthStatus = {
  status: "degraded",
  version: "0.1.0",
  warnings: [{ code: "ffmpeg_missing", message: "FFmpeg executable 'ffmpeg' was not found on PATH." }],
};

describe("HealthBanner", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders nothing when the app reports ok", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(okHealth));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<HealthBanner />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/health", expect.anything()));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a warning banner when the app reports degraded (FFmpeg missing)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(degradedHealth)));
    render(<HealthBanner />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/ffmpeg executable 'ffmpeg' was not found on path/i);
  });

  it("renders nothing when the health fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<HealthBanner />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/health", expect.anything()));
    expect(container).toBeEmptyDOMElement();
  });
});
