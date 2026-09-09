import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DefaultAudioSection } from "../components/settings/DefaultAudioSection";
import type { GlobalSettings } from "../types/settings";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const baseSettings: GlobalSettings = {
  enabled_targets: ["stereo"],
  language_allow_list: ["eng"],
  stereo_codec: "aac",
  stereo_bitrate_kbps: null,
  surround_codec: "ac3",
  surround_bitrate_kbps: 448,
  concurrency_limit: 1,
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
  api_key: "server-key",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

describe("DefaultAudioSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the current preferred default audio from a mocked GET", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          ...baseSettings,
          default_audio_language: "eng",
          default_audio_channel_tier: "5.1",
          auto_set_default_audio: true,
        }),
      ),
    );
    render(<DefaultAudioSection />);

    expect(await screen.findByLabelText(/preferred language/i)).toHaveValue("eng");
    expect(screen.getByLabelText(/preferred channel tier/i)).toHaveValue("5.1");
    const autoSetCheckbox = screen.getByRole("checkbox", {
      name: /automatically set the default audio track when downmixing/i,
    });
    expect(autoSetCheckbox).toBeChecked();
    expect(autoSetCheckbox).not.toBeDisabled();
  });

  it("renders the preferred language as a picker (select), not free text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<DefaultAudioSection />);

    const languageField = await screen.findByLabelText(/preferred language/i);
    expect(languageField.tagName).toBe("SELECT");
    expect(screen.getByRole("option", { name: /english \(eng\)/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /french \(fre\)/i })).toBeInTheDocument();
  });

  it("shows a saved language not in LANGUAGE_OPTIONS instead of silently falling back to None", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          ...baseSettings,
          default_audio_language: "wel",
          default_audio_channel_tier: "stereo",
        }),
      ),
    );
    render(<DefaultAudioSection />);

    expect(await screen.findByLabelText(/preferred language/i)).toHaveValue("wel");
    expect(screen.getByRole("option", { name: /wel \(not in list\)/i })).toBeInTheDocument();
  });

  it("renders unset language/channel-tier and a disabled, unchecked auto-set checkbox by default", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<DefaultAudioSection />);

    expect(await screen.findByLabelText(/preferred language/i)).toHaveValue("");
    expect(screen.getByLabelText(/preferred channel tier/i)).toHaveValue("");
    const autoSetCheckbox = screen.getByRole("checkbox", {
      name: /automatically set the default audio track when downmixing/i,
    });
    expect(autoSetCheckbox).not.toBeChecked();
    expect(autoSetCheckbox).toBeDisabled();
  });

  it("enables the auto-set checkbox once both language and channel tier are set, and saves via PUT", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(
          jsonResponse({
            ...baseSettings,
            default_audio_language: body.default_audio_language,
            default_audio_channel_tier: body.default_audio_channel_tier,
            auto_set_default_audio: body.auto_set_default_audio,
          }),
        );
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DefaultAudioSection />);
    await screen.findByLabelText(/preferred language/i);

    const autoSetCheckbox = screen.getByRole("checkbox", {
      name: /automatically set the default audio track when downmixing/i,
    });
    expect(autoSetCheckbox).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/preferred language/i), { target: { value: "eng" } });
    expect(autoSetCheckbox).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/preferred channel tier/i), { target: { value: "5.1" } });
    expect(autoSetCheckbox).not.toBeDisabled();

    fireEvent.click(autoSetCheckbox);
    expect(autoSetCheckbox).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: /save preferred default audio/i }));
    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();

    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(putCall).toBeDefined();
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody).toEqual({
      default_audio_language: "eng",
      default_audio_channel_tier: "5.1",
      auto_set_default_audio: true,
      default_audio_delay_minutes: 30,
    });

    expect(screen.getByLabelText(/preferred language/i)).toHaveValue("eng");
    expect(screen.getByLabelText(/preferred channel tier/i)).toHaveValue("5.1");
    expect(autoSetCheckbox).toBeChecked();
  });

  it("clears the auto-set checkbox when the language is cleared after being checked, and does not send an invalid combination", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(
          jsonResponse({
            ...baseSettings,
            default_audio_language: body.default_audio_language,
            default_audio_channel_tier: body.default_audio_channel_tier,
            auto_set_default_audio: body.auto_set_default_audio,
          }),
        );
      }
      return Promise.resolve(
        jsonResponse({
          ...baseSettings,
          default_audio_language: "eng",
          default_audio_channel_tier: "5.1",
          auto_set_default_audio: true,
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DefaultAudioSection />);
    const languageInput = await screen.findByLabelText(/preferred language/i);
    const autoSetCheckbox = screen.getByRole("checkbox", {
      name: /automatically set the default audio track when downmixing/i,
    });
    expect(autoSetCheckbox).toBeChecked();
    expect(autoSetCheckbox).not.toBeDisabled();

    fireEvent.change(languageInput, { target: { value: "" } });
    expect(autoSetCheckbox).not.toBeChecked();
    expect(autoSetCheckbox).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /save preferred default audio/i }));
    await screen.findByText(/saved\./i);

    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.default_audio_language).toBeNull();
    expect(putBody.auto_set_default_audio).toBe(false);
  });

  // --- default audio delay (COL-243) -----------------------------------------

  it("renders the default audio delay from a mocked GET, with its inline warning note", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ ...baseSettings, default_audio_delay_minutes: 45 })),
    );
    render(<DefaultAudioSection />);

    expect(await screen.findByLabelText(/default audio delay \(minutes\)/i)).toHaveValue(45);
    expect(
      screen.getByText(
        /durations shorter than 30 minutes may fail if your plex has not processed the new audio streams in the media file yet\./i,
      ),
    ).toBeInTheDocument();
  });

  it("defaults the default audio delay to 30 minutes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<DefaultAudioSection />);

    expect(await screen.findByLabelText(/default audio delay \(minutes\)/i)).toHaveValue(30);
  });

  it("saves an edited default audio delay via PUT and round-trips the response", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(
          jsonResponse({ ...baseSettings, default_audio_delay_minutes: body.default_audio_delay_minutes }),
        );
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DefaultAudioSection />);
    const delayInput = await screen.findByLabelText(/default audio delay \(minutes\)/i);

    fireEvent.change(delayInput, { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: /save preferred default audio/i }));
    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();

    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.default_audio_delay_minutes).toBe(90);
    expect(delayInput).toHaveValue(90);
  });

  it("rejects a negative default audio delay client-side without sending a PUT", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(baseSettings));
    vi.stubGlobal("fetch", fetchMock);

    render(<DefaultAudioSection />);
    const delayInput = await screen.findByLabelText(/default audio delay \(minutes\)/i);

    fireEvent.change(delayInput, { target: { value: "-1" } });
    fireEvent.click(screen.getByRole("button", { name: /save preferred default audio/i }));

    expect(
      await screen.findByText(/default audio delay must be a whole number of 0 or more\./i),
    ).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "PUT")).toBe(
      false,
    );
  });

  it("surfaces an API error from a failed save", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse({ detail: "invalid language code" }, 400));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DefaultAudioSection />);
    await screen.findByLabelText(/preferred language/i);
    fireEvent.click(screen.getByRole("button", { name: /save preferred default audio/i }));

    expect(await screen.findByText("invalid language code")).toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<DefaultAudioSection />);

    expect(await screen.findByText(/couldn't load settings: network down/i)).toBeInTheDocument();
  });
});
