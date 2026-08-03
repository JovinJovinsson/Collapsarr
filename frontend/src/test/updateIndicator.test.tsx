import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UpdateIndicator } from "../components/UpdateIndicator";
import type { UpdateCheckState } from "../types/updates";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const upToDate: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.2.3",
  latest_version_label: "v1.2.3",
  changelog: null,
  checked_at: "2026-08-02T10:00:00Z",
  update_available: false,
  dismissed_at: null,
  is_docker: false,
};

const updateAvailable: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.3.0",
  latest_version_label: "Collapsarr v1.3.0",
  changelog: "- added things",
  checked_at: "2026-08-02T10:00:00Z",
  update_available: true,
  dismissed_at: null,
  is_docker: false,
};

function renderIndicator() {
  return render(
    <MemoryRouter>
      <UpdateIndicator />
    </MemoryRouter>
  );
}

describe("UpdateIndicator", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders nothing when up to date", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(upToDate));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderIndicator();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/system/updates", expect.anything()));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows a neutral notice and links to /system/updates when an update is available", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(updateAvailable)));
    renderIndicator();

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(/update available/i);
    expect(notice).toHaveTextContent("v1.3.0");
    expect(notice).toHaveAttribute("href", "/system/updates");
  });

  it("is visually distinct from HealthBanner's alert-role, danger-toned styling", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(updateAvailable)));
    renderIndicator();

    const notice = await screen.findByRole("status");
    // Its own class, not health-banner's -- and role="status" (a polite,
    // informational live region), not role="alert" like HealthBanner --
    // since nothing is broken.
    expect(notice).toHaveClass("update-indicator");
    expect(notice.className).not.toMatch(/health-banner/);
  });

  it("renders nothing when the fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderIndicator();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/system/updates", expect.anything()));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while the fetch is still pending", () => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise(() => {})));
    const { container } = renderIndicator();

    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the update notice has been dismissed (COL-89)", async () => {
    const dismissed: UpdateCheckState = { ...updateAvailable, dismissed_at: "2026-08-02T11:00:00Z" };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(dismissed));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderIndicator();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/system/updates", expect.anything()));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
