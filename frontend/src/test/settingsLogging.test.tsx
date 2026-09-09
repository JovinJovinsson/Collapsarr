import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LoggingSection } from "../components/settings/LoggingSection";
import type { GlobalSettings } from "../types/settings";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const baseSettings: GlobalSettings = {
  enabled_targets: ["stereo"],
  language_allow_list: null,
  stereo_codec: "aac",
  stereo_bitrate_kbps: null,
  surround_codec: "ac3",
  surround_bitrate_kbps: 448,
  concurrency_limit: 2,
  ui_auth_enabled: false,
  auth_required: "local_bypass",
  auth_method: "forms",
  backup_interval_days: 7,
  backup_retention_days: 28,
  disk_space_warning_percent: 5,
  disk_space_error_percent: 2,
  update_channel: "stable",
  default_tracked: true,
  log_level: null,
  default_audio_language: null,
  default_audio_channel_tier: null,
  auto_set_default_audio: false,
  default_audio_delay_minutes: 30,
  recently_processed_window_minutes: 360,
  auto_queue_paused: false,
  auto_processing_paused: false,
  ignore_commentary_tracks: true,
  api_key: "server-generated-key",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

/**
 * Settings → Logging (COL-147): covers the `log_level` runtime-override
 * control moved verbatim off `GeneralSection` (where COL-130 originally
 * added it, and `settingsGeneral.test.tsx` originally covered it) onto its
 * own `LoggingSection`/`SettingsLoggingPage`. Assertions below are the same
 * ones `settingsGeneral.test.tsx` dropped -- same options, same
 * immediate-effect/no-restart/persists-across-restarts behavior, same
 * `PUT /api/settings` save call.
 */
describe("LoggingSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("displays Default (env) as the log level when unset from a mocked GET", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<LoggingSection />);

    expect(await screen.findByLabelText(/^level$/i)).toHaveValue("__default__");
  });

  it("displays a previously-set log level from a mocked GET", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ ...baseSettings, log_level: "DEBUG" })),
    );
    render(<LoggingSection />);

    expect(await screen.findByLabelText(/^level$/i)).toHaveValue("DEBUG");
  });

  it("saves the log_level via PUT when switched to Warning", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LoggingSection />);
    const logLevelSelect = await screen.findByLabelText(/^level$/i);
    fireEvent.change(logLevelSelect, { target: { value: "WARNING" } });
    fireEvent.click(screen.getByRole("button", { name: /save log level/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.log_level).toBe("WARNING");
  });

  it("clears a previously-set log_level override by selecting Default (env)", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse({ ...baseSettings, log_level: "DEBUG" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LoggingSection />);
    const logLevelSelect = await screen.findByLabelText(/^level$/i);
    expect(logLevelSelect).toHaveValue("DEBUG");
    fireEvent.change(logLevelSelect, { target: { value: "__default__" } });
    fireEvent.click(screen.getByRole("button", { name: /save log level/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.log_level).toBeNull();
  });

  it("surfaces an API error from a failed save", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse({ detail: "invalid log level" }, 400));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LoggingSection />);
    await screen.findByLabelText(/^level$/i);
    fireEvent.click(screen.getByRole("button", { name: /save log level/i }));

    expect(await screen.findByText("invalid log level")).toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<LoggingSection />);

    expect(await screen.findByText(/couldn't load settings: network down/i)).toBeInTheDocument();
  });
});
