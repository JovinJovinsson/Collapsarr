import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getStoredApiKey } from "../api/client";
import { fetchUpdateStatus, recheckUpdateStatus } from "../api/updates";
import { GeneralSection } from "../components/settings/GeneralSection";
import { UpdateIndicator } from "../components/UpdateIndicator";
import { UpdatesProvider } from "../components/UpdatesProvider";
import type { GlobalSettings } from "../types/settings";
import type { UpdateCheckState } from "../types/updates";

// COL-196: `fetchUpdateStatus`/`recheckUpdateStatus` are mocked at the
// module level rather than via the raw `fetch` mock used elsewhere in this
// file, so the assertions below are "was the recheck triggered"/"what state
// did it push", not "which URL got hit" -- keeps the tests decoupled from
// those endpoints' own request shapes. `fetchUpdateStatus` is mocked too
// (COL-196 code review) because `GeneralSection` now reads the shared Update
// Check state via `useUpdates()`, which requires wrapping it in
// `UpdatesProvider` -- whose own initial-mount fetch needs a resolved value
// here, same as `UpdateIndicator`'s in `updateIndicator.test.tsx`.
vi.mock("../api/updates", () => ({
  fetchUpdateStatus: vi.fn(),
  recheckUpdateStatus: vi.fn(),
}));

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const baseUpdateCheckState: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.2.3",
  latest_version_label: "v1.2.3",
  changelog: null,
  checked_at: "2026-08-02T10:00:00Z",
  update_available: false,
  dismissed_at: null,
  install_method: "pipx",
};

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
  api_key: "server-generated-key",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

/**
 * GeneralSection renders a `Link` to the Updates page (COL-88), so every
 * render needs a Router context -- mirrors `updateIndicator.test.tsx`. It
 * also reads the shared Update Check state via `useUpdates()` (COL-196 code
 * review), so it needs `UpdatesProvider` too, mirroring
 * `healthBanner.test.tsx`'s `HealthProvider` wrapping.
 */
function renderGeneralSection() {
  return render(
    <MemoryRouter>
      <UpdatesProvider>
        <GeneralSection />
      </UpdatesProvider>
    </MemoryRouter>
  );
}

describe("GeneralSection", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(fetchUpdateStatus).mockReset().mockResolvedValue(baseUpdateCheckState);
    vi.mocked(recheckUpdateStatus).mockReset().mockResolvedValue(baseUpdateCheckState);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("displays the server API key and current general settings from a mocked GET", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    expect(await screen.findByLabelText(/server api key/i)).toHaveValue("server-generated-key");
    expect(screen.getByLabelText(/concurrency limit/i)).toHaveValue(2);
    expect(screen.getByLabelText(/stereo codec/i)).toHaveValue("aac");
    expect(screen.getByLabelText(/surround bitrate/i)).toHaveValue(448);
    expect(screen.getByRole("checkbox", { name: /require the api key/i })).not.toBeChecked();
    expect(screen.getByLabelText(/login requirement/i)).toHaveValue("local_bypass");
    expect(screen.getByLabelText(/sign-in method/i)).toHaveValue("forms");
    expect(screen.getByLabelText(/warning threshold/i)).toHaveValue(5);
    expect(screen.getByLabelText(/critical threshold/i)).toHaveValue(2);
  });

  it("shows a restart-required hint under the concurrency limit field", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    await screen.findByLabelText(/concurrency limit/i);
    expect(screen.getByText(/restart collapsarr for a change to take effect/i)).toBeInTheDocument();
  });

  it("lays out the Authentication & concurrency and Update channel fields with the shared full-width grid (COL-188)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    const authRequiredSelect = await screen.findByLabelText(/login requirement/i);
    const authMethodSelect = screen.getByLabelText(/sign-in method/i);
    const concurrencyInput = screen.getByLabelText(/concurrency limit/i);
    const windowInput = screen.getByLabelText(/recently-processed window/i);
    const channelSelect = screen.getByLabelText(/release channel/i);

    // Same grid pattern as the "Disk space alerts" / "Advanced" panels: each
    // field lives in a `.form-grid` > `.form-field` wrapper, not the old
    // fixed-width `.form-field--narrow`, so fields flow across the full
    // panel width instead of being capped at a narrow column.
    for (const field of [authRequiredSelect, authMethodSelect, concurrencyInput, windowInput, channelSelect]) {
      const wrapper = field.closest(".form-field");
      expect(wrapper).not.toBeNull();
      expect(wrapper).not.toHaveClass("form-field--narrow");
      expect(wrapper?.parentElement).toHaveClass("form-grid");
    }
  });

  it("saves the disk-space thresholds via PUT with the edited values", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const warningInput = await screen.findByLabelText(/warning threshold/i);
    const errorInput = screen.getByLabelText(/critical threshold/i);
    fireEvent.change(warningInput, { target: { value: "10" } });
    fireEvent.change(errorInput, { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.disk_space_warning_percent).toBe(10);
    expect(putBody.disk_space_error_percent).toBe(3);
  });

  it("validates the disk-space warning threshold before saving", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    const warningInput = await screen.findByLabelText(/warning threshold/i);
    fireEvent.change(warningInput, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(
      await screen.findByText(/disk space warning threshold must be a percentage greater than 0/i),
    ).toBeInTheDocument();
  });

  it("validates the disk-space critical threshold before saving", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    const errorInput = await screen.findByLabelText(/critical threshold/i);
    fireEvent.change(errorInput, { target: { value: "150" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(
      await screen.findByText(/disk space critical threshold must be a percentage greater than 0/i),
    ).toBeInTheDocument();
  });

  it("saves the auth_method via PUT when switched to HTTP Basic", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const authMethodSelect = await screen.findByLabelText(/sign-in method/i);
    fireEvent.change(authMethodSelect, { target: { value: "basic" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.auth_method).toBe("basic");
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

    renderGeneralSection();
    const authRequiredSelect = await screen.findByLabelText(/login requirement/i);
    fireEvent.change(authRequiredSelect, { target: { value: "enabled" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.auth_required).toBe("enabled");
  });

  it("displays the current update channel from a mocked GET", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ ...baseSettings, update_channel: "beta" })),
    );
    renderGeneralSection();

    expect(await screen.findByLabelText(/release channel/i)).toHaveValue("beta");
  });

  it("saves the update_channel via PUT when switched to beta", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const channelSelect = await screen.findByLabelText(/release channel/i);
    fireEvent.change(channelSelect, { target: { value: "beta" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.update_channel).toBe("beta");
  });

  it("triggers an immediate update recheck when a save changes the release channel (COL-196)", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const channelSelect = await screen.findByLabelText(/release channel/i);
    fireEvent.change(channelSelect, { target: { value: "beta" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    await waitFor(() => expect(recheckUpdateStatus).toHaveBeenCalledTimes(1));
  });

  it("does not trigger an update recheck when General settings save without a channel change (COL-196)", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const concurrencyInput = await screen.findByLabelText(/concurrency limit/i);
    fireEvent.change(concurrencyInput, { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    expect(recheckUpdateStatus).not.toHaveBeenCalled();
  });

  it(
    "reflects a channel-changing save's recheck result in a concurrently mounted UpdateIndicator, " +
      "with no page reload (COL-196 code review)",
    async () => {
      const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
        if ((init?.method ?? "GET") === "PUT") {
          const body = JSON.parse(String(init?.body));
          return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
        }
        return Promise.resolve(jsonResponse(baseSettings));
      });
      vi.stubGlobal("fetch", fetchMock);

      const refreshedState: UpdateCheckState = {
        ...baseUpdateCheckState,
        latest_version: "v1.3.0-beta.1",
        update_available: true,
      };
      vi.mocked(recheckUpdateStatus).mockReset().mockResolvedValue(refreshedState);

      // UpdateIndicator mounted as a sibling under the same UpdatesProvider,
      // mirroring how AppShell mounts it once (outside the <Outlet />) for
      // the whole SPA session, alongside whatever page (here, GeneralSection)
      // is currently routed into that outlet.
      render(
        <MemoryRouter>
          <UpdatesProvider>
            <UpdateIndicator />
            <GeneralSection />
          </UpdatesProvider>
        </MemoryRouter>
      );

      expect(screen.queryByRole("status")).not.toBeInTheDocument();

      const channelSelect = await screen.findByLabelText(/release channel/i);
      fireEvent.change(channelSelect, { target: { value: "beta" } });
      fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

      expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
      const notice = await screen.findByRole("status");
      expect(notice).toHaveTextContent(/update available/i);
      expect(notice).toHaveTextContent("v1.3.0-beta.1");
    }
  );

  it("saves the browser-stored API key to localStorage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    const browserKeyInput = await screen.findByLabelText(/this browser's stored key/i);
    fireEvent.change(browserKeyInput, { target: { value: "my-local-key" } });
    fireEvent.click(screen.getByRole("button", { name: /save browser key/i }));

    expect(getStoredApiKey()).toBe("my-local-key");
  });

  it("copies the server key into the browser-stored key with one click", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    await screen.findByLabelText(/server api key/i);
    fireEvent.click(screen.getByRole("button", { name: /use server key/i }));

    expect(getStoredApiKey()).toBe("server-generated-key");
    expect(screen.getByLabelText(/this browser's stored key/i)).toHaveValue("server-generated-key");
  });

  it("validates concurrency limit before saving", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

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

    renderGeneralSection();
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

    renderGeneralSection();
    const surroundBitrateInput = await screen.findByLabelText(/surround bitrate/i);
    fireEvent.change(surroundBitrateInput, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    await screen.findByText(/saved\./i);
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.surround_bitrate_kbps).toBeNull();
  });

  it("displays the current default_tracked value and saves changes via PUT", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse({ ...baseSettings, default_tracked: false }));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const toggleCheckbox = await screen.findByRole("checkbox", { name: /default tracked for new library items/i });
    expect(toggleCheckbox).not.toBeChecked();

    fireEvent.click(toggleCheckbox);
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.default_tracked).toBe(true);
  });

  it("surfaces an API error from a failed save", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse({ detail: "invalid codec" }, 400));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    await screen.findByLabelText(/server api key/i);
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText("invalid codec")).toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    renderGeneralSection();

    expect(await screen.findByText(/couldn't load settings: network down/i)).toBeInTheDocument();
  });

  it("displays the recently-processed window from a mocked GET", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    expect(await screen.findByLabelText(/recently-processed window/i)).toHaveValue(360);
  });

  it("saves the recently-processed window via PUT with the edited value", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const windowInput = await screen.findByLabelText(/recently-processed window/i);
    fireEvent.change(windowInput, { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.recently_processed_window_minutes).toBe(120);
  });

  it("allows 0 as a valid value for the recently-processed window", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...baseSettings, ...body }));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    const windowInput = await screen.findByLabelText(/recently-processed window/i);
    fireEvent.change(windowInput, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.recently_processed_window_minutes).toBe(0);
  });

  it("validates the recently-processed window before saving", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    const windowInput = await screen.findByLabelText(/recently-processed window/i);
    fireEvent.change(windowInput, { target: { value: "-10" } });
    fireEvent.click(screen.getByRole("button", { name: /save general settings/i }));

    expect(await screen.findByText(/recently-processed window must be a whole number of 0 or more/i)).toBeInTheDocument();
  });
});

describe("GeneralSection credential controls (COL-55)", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("rejects a password change with the wrong current password", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/auth/change-password") {
        return Promise.resolve(jsonResponse({ detail: "Current password is incorrect." }, 401));
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    fireEvent.change(await screen.findByLabelText(/current password/i), {
      target: { value: "wrong password" },
    });
    fireEvent.change(screen.getByLabelText(/^new password$/i), { target: { value: "a new password" } });
    fireEvent.change(screen.getByLabelText(/confirm new password/i), {
      target: { value: "a new password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /change password/i }));

    expect(await screen.findByText(/current password is incorrect/i)).toBeInTheDocument();
  });

  it("changes the password with the correct current password", async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<ReturnType<typeof jsonResponse>>>(
      (url) => {
        if (url === "/api/auth/change-password") {
          return Promise.resolve(
            jsonResponse({ needs_setup: false, authenticated: true, auth_method: "forms" }),
          );
        }
        return Promise.resolve(jsonResponse(baseSettings));
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderGeneralSection();
    fireEvent.change(await screen.findByLabelText(/current password/i), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.change(screen.getByLabelText(/^new password$/i), { target: { value: "a new password" } });
    fireEvent.change(screen.getByLabelText(/confirm new password/i), {
      target: { value: "a new password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /change password/i }));

    expect(await screen.findByText(/password changed\./i)).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([url]) => url === "/api/auth/change-password");
    const body = JSON.parse(String((call?.[1] as RequestInit).body));
    expect(body).toEqual({ current_password: "correct horse battery staple", new_password: "a new password" });
  });

  it("requires the new password and confirmation to match before submitting", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseSettings)));
    renderGeneralSection();

    fireEvent.change(await screen.findByLabelText(/current password/i), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.change(screen.getByLabelText(/^new password$/i), { target: { value: "one" } });
    fireEvent.change(screen.getByLabelText(/confirm new password/i), { target: { value: "two" } });
    fireEvent.click(screen.getByRole("button", { name: /change password/i }));

    expect(await screen.findByText(/do not match/i)).toBeInTheDocument();
  });

  it("logs out everywhere and redirects to /login", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/auth/logout-everywhere") {
        return Promise.resolve(
          jsonResponse({ needs_setup: false, authenticated: false, auth_method: "forms" }),
        );
      }
      return Promise.resolve(jsonResponse(baseSettings));
    });
    vi.stubGlobal("fetch", fetchMock);
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/settings", assign });

    renderGeneralSection();
    fireEvent.click(await screen.findByRole("button", { name: /log out everywhere/i }));

    await screen.findByRole("button", { name: /log out everywhere/i });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/logout-everywhere",
      expect.objectContaining({ method: "POST" }),
    );
    expect(assign).toHaveBeenCalledWith("/login");
  });
});
