import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TargetsSection } from "../components/settings/TargetsSection";
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
  api_key: "server-key",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

describe("TargetsSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the current targets and allow-list from a mocked GET", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    render(<TargetsSection />);

    const stereoCheckbox = await screen.findByRole("checkbox", { name: /stereo/i });
    expect(stereoCheckbox).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "2.1" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "5.1" })).not.toBeChecked();
    expect(screen.getByLabelText(/language allow-list/i)).toHaveValue("eng");
  });

  it("toggles a target and saves via PUT", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(
          jsonResponse({ ...baseSettings, enabled_targets: body.enabled_targets, language_allow_list: body.language_allow_list }),
        );
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TargetsSection />);
    await screen.findByLabelText(/language allow-list/i);

    fireEvent.click(screen.getByRole("checkbox", { name: "5.1" }));
    fireEvent.click(screen.getByRole("button", { name: /save targets/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();

    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(putCall).toBeDefined();
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.enabled_targets.sort()).toEqual(["5.1", "stereo"].sort());
  });

  it("parses the language allow-list field into an array, dropping blanks", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse(baseSettings));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TargetsSection />);
    const allowListInput = await screen.findByLabelText(/language allow-list/i);
    fireEvent.change(allowListInput, { target: { value: "eng, fre ,  , spa" } });
    fireEvent.click(screen.getByRole("button", { name: /save targets/i }));

    await screen.findByText(/saved\./i);
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.language_allow_list.sort()).toEqual(["eng", "fre", "spa"]);
  });

  it("sends null for a blank language allow-list (allow every language)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(baseSettings));
    vi.stubGlobal("fetch", fetchMock);

    render(<TargetsSection />);
    const allowListInput = await screen.findByLabelText(/language allow-list/i);
    fireEvent.change(allowListInput, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save targets/i }));

    await screen.findByText(/saved\./i);
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.language_allow_list).toBeNull();
  });

  it("surfaces an API error from a failed save", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse({ detail: "concurrency exceeded" }, 400));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TargetsSection />);
    await screen.findByLabelText(/language allow-list/i);
    fireEvent.click(screen.getByRole("button", { name: /save targets/i }));

    expect(await screen.findByText("concurrency exceeded")).toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<TargetsSection />);

    expect(await screen.findByText(/couldn't load settings: network down/i)).toBeInTheDocument();
  });
});
