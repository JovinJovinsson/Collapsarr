import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SchedulerSection } from "../components/settings/SchedulerSection";
import type { GlobalSettings } from "../types/settings";
import type { ScheduledTask } from "../types/tasks";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const sampleSettings: GlobalSettings = {
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
  api_key: "abc123",
  created_at: "2026-07-27T00:00:00Z",
  updated_at: "2026-07-27T00:00:00Z",
};

const libraryScan: ScheduledTask = {
  name: "Library scan",
  interval_label: "Every 6 hours",
  next_run_at: "2026-08-09T12:00:00Z",
  last_run_at: "2026-08-09T06:00:00Z",
  scheduler_enabled: true,
};

const healthChecks: ScheduledTask = {
  name: "Health checks",
  interval_label: "Every 5 minutes",
  next_run_at: null,
  last_run_at: "2026-08-09T09:55:00Z",
  scheduler_enabled: false,
};

const backupsTask: ScheduledTask = {
  name: "Backups",
  interval_label: "Every 7 days",
  next_run_at: null,
  last_run_at: null,
  scheduler_enabled: false,
};

const updateCheck: ScheduledTask = {
  name: "Update check",
  interval_label: "Every 24 hours",
  next_run_at: "2026-08-10T09:00:00Z",
  last_run_at: "2026-08-09T09:00:00Z",
  scheduler_enabled: true,
};

const allTasks: ScheduledTask[] = [libraryScan, healthChecks, backupsTask, updateCheck];

/** Routes a mocked `fetch` by URL/method: `/api/settings` vs. `/api/system/tasks`. */
function stubFetch(options: {
  settings?: GlobalSettings;
  tasks?: ScheduledTask[];
  onSettingsPut?: (body: unknown) => { ok: boolean; status: number; json: () => Promise<unknown> };
}) {
  const { settings = sampleSettings, tasks = allTasks, onSettingsPut } = options;

  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/settings" && method === "PUT") {
      const body: unknown = init?.body ? JSON.parse(init.body as string) : {};
      return Promise.resolve(onSettingsPut ? onSettingsPut(body) : jsonResponse({ ...settings, ...(body as object) }));
    }
    if (url === "/api/settings") {
      return Promise.resolve(jsonResponse(settings));
    }
    if (url === "/api/system/tasks") {
      return Promise.resolve(jsonResponse(tasks));
    }
    return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SchedulerSection", () => {
  // --- Backup schedule (moved verbatim from BackupsPage, COL-66 / COL-146) ----

  it("renders the persisted interval/retention values from a mocked GET", async () => {
    stubFetch({ settings: { ...sampleSettings, backup_interval_days: 3, backup_retention_days: 14 } });
    render(<SchedulerSection />);

    expect(await screen.findByLabelText(/backup interval/i)).toHaveValue(3);
    expect(screen.getByLabelText(/backup retention/i)).toHaveValue(14);
  });

  it("saves edited interval/retention via PUT and persists across reload", async () => {
    const fetchMock = stubFetch({});
    render(<SchedulerSection />);

    const intervalInput = await screen.findByLabelText(/backup interval/i);
    const retentionInput = screen.getByLabelText(/backup retention/i);
    fireEvent.change(intervalInput, { target: { value: "10" } });
    fireEvent.change(retentionInput, { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: /save schedule/i }));

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/settings" && (init as RequestInit | undefined)?.method === "PUT",
    );
    expect(putCall).toBeDefined();
    const body: unknown = JSON.parse((putCall?.[1] as RequestInit).body as string);
    expect(body).toEqual({ backup_interval_days: 10, backup_retention_days: 30 });
    expect(intervalInput).toHaveValue(10);
    expect(retentionInput).toHaveValue(30);
  });

  it("rejects a non-positive interval before saving, without calling PUT", async () => {
    const fetchMock = stubFetch({});
    render(<SchedulerSection />);

    const intervalInput = await screen.findByLabelText(/backup interval/i);
    fireEvent.change(intervalInput, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: /save schedule/i }));

    expect(await screen.findByText(/must be a whole number of 1 or more/i)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => url === "/api/settings" && (init as RequestInit | undefined)?.method === "PUT",
      ),
    ).toBe(false);
  });

  it("surfaces an error when saving the schedule fails", async () => {
    stubFetch({ onSettingsPut: () => jsonResponse({ detail: "invalid retention" }, 422) });
    render(<SchedulerSection />);

    fireEvent.click(await screen.findByRole("button", { name: /save schedule/i }));

    expect(await screen.findByText("invalid retention")).toBeInTheDocument();
  });

  it("renders an error state when the initial schedule load fails", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/settings") return Promise.reject(new Error("network down"));
      if (url === "/api/system/tasks") return Promise.resolve(jsonResponse(allTasks));
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SchedulerSection />);

    expect(await screen.findByText(/couldn't load the backup schedule: network down/i)).toBeInTheDocument();
  });

  // --- Read-only cadence for the other periodic tasks (COL-146) ---------------

  it("shows the cadence of Library Scan, Health Checks, and Update Check from a mocked GET", async () => {
    stubFetch({});
    render(<SchedulerSection />);

    expect(await screen.findByText("Library scan")).toBeInTheDocument();
    expect(screen.getByText("Every 6 hours")).toBeInTheDocument();
    expect(screen.getByText("Health checks")).toBeInTheDocument();
    expect(screen.getByText("Every 5 minutes")).toBeInTheDocument();
    expect(screen.getByText("Update check")).toBeInTheDocument();
    expect(screen.getByText("Every 24 hours")).toBeInTheDocument();
  });

  it("excludes the Backups row from the read-only cadence section", async () => {
    stubFetch({});
    render(<SchedulerSection />);

    await screen.findByText("Library scan");
    // "Backups" only appears via the editable panel above (its heading), not
    // as a read-only cadence row -- it already has its own editable panel.
    expect(screen.queryByText("Every 7 days")).not.toBeInTheDocument();
  });

  it("notes that the read-only tasks aren't yet editable", async () => {
    stubFetch({});
    render(<SchedulerSection />);

    expect(await screen.findByText(/aren.t configurable yet/i)).toBeInTheDocument();
  });

  it("renders an error state when the task cadence load fails", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/settings") return Promise.resolve(jsonResponse(sampleSettings));
      if (url === "/api/system/tasks") return Promise.reject(new Error("network down"));
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SchedulerSection />);

    expect(await screen.findByText(/couldn't load task cadence: network down/i)).toBeInTheDocument();
  });
});
