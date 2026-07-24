import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OnboardingPanel } from "../components/OnboardingPanel";
import type { ArrInstance } from "../types/instances";
import type { GlobalSettings } from "../types/settings";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const settings: GlobalSettings = {
  enabled_targets: ["stereo"],
  language_allow_list: null,
  stereo_codec: "aac",
  stereo_bitrate_kbps: null,
  surround_codec: "ac3",
  surround_bitrate_kbps: 448,
  concurrency_limit: 1,
  ui_auth_enabled: false,
  auth_required: "local_bypass",
  auth_method: "forms",
  api_key: "onboarding-server-key",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

const instance: ArrInstance = {
  id: 1,
  name: "Sonarr",
  type: "sonarr",
  base_url: "http://localhost:8989",
  api_key: "instance-key",
  status: "ok",
  status_error: null,
  status_checked_at: null,
  version: null,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

function stubFetch(instances: ArrInstance[]) {
  const fetchMock = vi.fn((url: string) => {
    if (url === "/api/settings") return Promise.resolve(jsonResponse(settings));
    if (url === "/api/instances") return Promise.resolve(jsonResponse(instances));
    return Promise.reject(new Error(`Unhandled request in test mock: ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={["/wanted"]}>
      <Routes>
        <Route path="/wanted" element={<OnboardingPanel />} />
        <Route path="/settings" element={<div>Settings view</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

/**
 * Covers COL-54's AC: the onboarding panel renders with the auto-generated
 * API key and a working link to instance configuration while the install is
 * unconfigured, is dismissible and stays dismissed, and gets out of the way
 * once an arr instance is configured. Prior art: `settingsPage.test.tsx`
 * (mocked-fetch render pattern) and `apiClient.test.ts` (localStorage
 * persistence pattern).
 */
describe("OnboardingPanel", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("renders the auto-generated API key and a working link to instance configuration when unconfigured", async () => {
    stubFetch([]);
    renderPanel();

    expect(await screen.findByText(/welcome to collapsarr/i)).toBeInTheDocument();
    expect(screen.getByText("onboarding-server-key")).toBeInTheDocument();

    const link = screen.getByRole("link", { name: /connect your first sonarr or radarr instance/i });
    expect(link).toHaveAttribute("href", "/settings");

    fireEvent.click(link);
    expect(await screen.findByText("Settings view")).toBeInTheDocument();
  });

  it("renders nothing once at least one arr instance is configured", async () => {
    const fetchMock = stubFetch([instance]);
    renderPanel();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/instances", expect.anything()));
    expect(screen.queryByText(/welcome to collapsarr/i)).not.toBeInTheDocument();
  });

  it("dismisses the panel and keeps it dismissed across remounts", async () => {
    stubFetch([]);
    const { unmount } = renderPanel();

    expect(await screen.findByText(/welcome to collapsarr/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));

    expect(screen.queryByText(/welcome to collapsarr/i)).not.toBeInTheDocument();
    expect(localStorage.getItem("collapsarr.onboardingDismissed")).toBe("true");

    unmount();
    renderPanel();

    expect(screen.queryByText(/welcome to collapsarr/i)).not.toBeInTheDocument();
  });

  it("renders nothing on a fetch error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    );
    renderPanel();

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByText(/welcome to collapsarr/i)).not.toBeInTheDocument();
  });
});
