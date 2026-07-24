import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getStoredApiKey } from "../api/client";
import { GeneralSection } from "../components/settings/GeneralSection";
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
  api_key: "server-generated-key",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

describe("GeneralSection", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("displays the server API key and current general settings from a mocked GET", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<GeneralSection />);

    expect(await screen.findByLabelText(/server api key/i)).toHaveValue("server-generated-key");
    expect(screen.getByLabelText(/concurrency limit/i)).toHaveValue(2);
    expect(screen.getByLabelText(/stereo codec/i)).toHaveValue("aac");
    expect(screen.getByLabelText(/surround bitrate/i)).toHaveValue(448);
    expect(screen.getByRole("checkbox", { name: /require the api key/i })).not.toBeChecked();
    expect(screen.getByLabelText(/login requirement/i)).toHaveValue("local_bypass");
  });

  it("saves the auth_required mode via PUT when switched to always-required", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<GeneralSection />);
    const authRequiredSelect = await screen.findByLabelText(/login requirement/i);
    fireEvent.change(authRequiredSelect, { target: { value: "enabled" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.auth_required).toBe("enabled");
  });

  it("saves the browser-stored API key to localStorage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<GeneralSection />);

    const browserKeyInput = await screen.findByLabelText(/this browser's stored key/i);
    fireEvent.change(browserKeyInput, { target: { value: "my-local-key" } });
    fireEvent.click(screen.getByRole("button", { name: /save browser key/i }));

    expect(getStoredApiKey()).toBe("my-local-key");
  });

  it("copies the server key into the browser-stored key with one click", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<GeneralSection />);

    await screen.findByLabelText(/server api key/i);
    fireEvent.click(screen.getByRole("button", { name: /use server key/i }));

    expect(getStoredApiKey()).toBe("server-generated-key");
    expect(screen.getByLabelText(/this browser's stored key/i)).toHaveValue("server-generated-key");
  });

  it("validates concurrency limit before saving", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<GeneralSection />);

    const concurrencyInput = await screen.findByLabelText(/concurrency limit/i);
    fireEvent.change(concurrencyInput, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/concurrency limit must be a whole number of 1 or more/i)).toBeInTheDocument();
  });

  it("saves general settings via PUT with the edited values", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<GeneralSection />);
    const concurrencyInput = await screen.findByLabelText(/concurrency limit/i);
    fireEvent.change(concurrencyInput, { target: { value: "4" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /require the api key/i }));
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.concurrency_limit).toBe(4);
    expect(putBody.ui_auth_enabled).toBe(true);
  });

  it("clears a bitrate override by sending null when the field is blanked", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse(baseSettings));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<GeneralSection />);
    const surroundBitrateInput = await screen.findByLabelText(/surround bitrate/i);
    fireEvent.change(surroundBitrateInput, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    await screen.findByText(/saved\./i);
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.surround_bitrate_kbps).toBeNull();
  });

  it("surfaces an API error from a failed save", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse({ detail: "invalid codec" }, 400));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<GeneralSection />);
    await screen.findByLabelText(/server api key/i);
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText("invalid codec")).toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<GeneralSection />);

    expect(await screen.findByText(/couldn't load settings: network down/i)).toBeInTheDocument();
  });
});
