import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsPage } from "../pages/SettingsPage";
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
  api_key: "server-key",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

/**
 * Smoke test: renders the composed Settings view (COL-33) against a mocked
 * API and checks each section (Instances / Targets / General) mounts and
 * shows its populated or empty state -- the deeper per-section behaviour
 * (CRUD, validation, error surfacing) is covered by
 * `settingsInstances.test.tsx`, `settingsTargets.test.tsx`, and
 * `settingsGeneral.test.tsx`.
 */
describe("SettingsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("mounts all three settings sections against a mocked API response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url === "/api/instances") return Promise.resolve(jsonResponse([]));
        if (url === "/api/settings") return Promise.resolve(jsonResponse(settings));
        return Promise.reject(new Error(`Unhandled request in test mock: ${url}`));
      }),
    );

    render(<SettingsPage />);

    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(await screen.findByText(/no arr instances configured yet/i)).toBeInTheDocument();
    expect(await screen.findByRole("checkbox", { name: /stereo/i })).toBeInTheDocument();
    expect(await screen.findByLabelText(/server api key/i)).toHaveValue("server-key");
  });
});
