import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BackupsPage } from "../pages/BackupsPage";
import type { Backup, BackupList } from "../types/backups";
import type { GlobalSettings } from "../types/settings";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const sampleBackup: Backup = {
  id: "manual/collapsarr_backup_v0.1.0_2026.07.27_10.00.00.zip",
  name: "collapsarr_backup_v0.1.0_2026.07.27_10.00.00.zip",
  type: "manual",
  size: 2048,
  created_at: "2026-07-27T10:00:00Z",
};

const emptyList: BackupList = { supported: true, backups: [] };
const listWithOne: BackupList = { supported: true, backups: [sampleBackup] };

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
  api_key: "abc123",
  created_at: "2026-07-27T00:00:00Z",
  updated_at: "2026-07-27T00:00:00Z",
};

/** Routes a mocked `fetch` by URL/method: `/api/system/backup` vs. `/api/settings`. */
function stubFetch(options: {
  backups?: BackupList;
  settings?: GlobalSettings;
  onBackupPost?: () => { ok: boolean; status: number; json: () => Promise<unknown> };
  onSettingsPut?: (body: unknown) => { ok: boolean; status: number; json: () => Promise<unknown> };
}) {
  const {
    backups = emptyList,
    settings = sampleSettings,
    onBackupPost,
    onSettingsPut,
  } = options;

  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/system/backup" && method === "POST") {
      return Promise.resolve(onBackupPost ? onBackupPost() : jsonResponse(sampleBackup, 202));
    }
    if (url === "/api/system/backup") {
      return Promise.resolve(jsonResponse(backups));
    }
    if (url === "/api/settings" && method === "PUT") {
      const body: unknown = init?.body ? JSON.parse(init.body as string) : {};
      return Promise.resolve(onSettingsPut ? onSettingsPut(body) : jsonResponse({ ...settings, ...(body as object) }));
    }
    if (url === "/api/settings") {
      return Promise.resolve(jsonResponse(settings));
    }
    return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BackupsPage", () => {
  it("renders the heading and existing backups from a mocked GET", async () => {
    stubFetch({ backups: listWithOne });
    render(<BackupsPage />);

    expect(await screen.findByRole("heading", { name: "Backups" })).toBeInTheDocument();
    expect(await screen.findByText(sampleBackup.name)).toBeInTheDocument();
    // Size rendered human-readable, type labelled.
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByText("Manual")).toBeInTheDocument();
  });

  it("shows an empty state and a Backup now button when there are no backups", async () => {
    stubFetch({ backups: emptyList });
    render(<BackupsPage />);

    expect(await screen.findByText(/no backups yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /backup now/i })).toBeInTheDocument();
  });

  it("creates a backup via POST and shows it in the refreshed list", async () => {
    let backupCreated = false;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/system/backup" && method === "POST") {
        backupCreated = true;
        return Promise.resolve(jsonResponse(sampleBackup, 202));
      }
      if (url === "/api/system/backup") {
        return Promise.resolve(jsonResponse(backupCreated ? listWithOne : emptyList));
      }
      if (url === "/api/settings") {
        return Promise.resolve(jsonResponse(sampleSettings));
      }
      return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /backup now/i }));

    expect(await screen.findByText(sampleBackup.name)).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(([, i]) => (i as RequestInit | undefined)?.method === "POST");
    expect(postCall?.[0]).toBe("/api/system/backup");
  });

  it("surfaces an error when creating a backup fails", async () => {
    stubFetch({
      backups: emptyList,
      onBackupPost: () => jsonResponse({ detail: "disk full" }, 500),
    });

    render(<BackupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /backup now/i }));

    expect(await screen.findByText("disk full")).toBeInTheDocument();
  });

  it("shows the unavailable state (no controls) when the DB isn't file-based SQLite", async () => {
    stubFetch({ backups: { supported: false, backups: [] } });
    render(<BackupsPage />);

    expect(await screen.findByText(/unavailable for this database configuration/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /backup now/i })).not.toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/system/backup") return Promise.reject(new Error("network down"));
      if (url === "/api/settings") return Promise.resolve(jsonResponse(sampleSettings));
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<BackupsPage />);

    expect(await screen.findByText(/couldn't load backups: network down/i)).toBeInTheDocument();
  });

  it("displays the created timestamp in local time", async () => {
    stubFetch({ backups: listWithOne });
    render(<BackupsPage />);

    const row = (await screen.findByText(sampleBackup.name)).closest("tr");
    expect(row).not.toBeNull();
    const expected = new Date(sampleBackup.created_at).toLocaleString();
    expect(within(row as HTMLElement).getByText(expected)).toBeInTheDocument();
  });

  // --- Backup schedule (COL-66) ------------------------------------------------

  it("renders the persisted interval/retention values from a mocked GET", async () => {
    stubFetch({ settings: { ...sampleSettings, backup_interval_days: 3, backup_retention_days: 14 } });
    render(<BackupsPage />);

    expect(await screen.findByLabelText(/backup interval/i)).toHaveValue(3);
    expect(screen.getByLabelText(/backup retention/i)).toHaveValue(14);
  });

  it("saves edited interval/retention via PUT and persists across reload", async () => {
    const fetchMock = stubFetch({});
    render(<BackupsPage />);

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
    render(<BackupsPage />);

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
    render(<BackupsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /save schedule/i }));

    expect(await screen.findByText("invalid retention")).toBeInTheDocument();
  });

  // --- Download action (COL-64) ------------------------------------------------

  it("downloads a backup via the per-row Download action", async () => {
    const blob = new Blob(["fake zip bytes"], { type: "application/zip" });
    const fetchMock = vi.fn((url: string) => {
      if (url === `/api/system/backup/${sampleBackup.id}/download`) {
        return Promise.resolve({ ok: true, status: 200, blob: () => Promise.resolve(blob) });
      }
      if (url === "/api/system/backup") {
        return Promise.resolve(jsonResponse(listWithOne));
      }
      if (url === "/api/settings") {
        return Promise.resolve(jsonResponse(sampleSettings));
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    // jsdom's `URL` has no `createObjectURL`/`revokeObjectURL` at all (unlike a
    // real browser), so they're defined fresh here rather than `vi.spyOn`-ed
    // onto an existing method, then removed again in `finally`.
    const createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {
      value: createObjectURL,
      configurable: true,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      value: revokeObjectURL,
      configurable: true,
    });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

    try {
      render(<BackupsPage />);
      fireEvent.click(await screen.findByRole("button", { name: /^download$/i }));

      await waitFor(() => expect(clickSpy).toHaveBeenCalledTimes(1));
      expect(createObjectURL).toHaveBeenCalledWith(blob);
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");

      const downloadCall = fetchMock.mock.calls.find(([url]) => (url as string).endsWith("/download"));
      expect(downloadCall?.[0]).toBe(`/api/system/backup/${sampleBackup.id}/download`);
    } finally {
      delete (URL as { createObjectURL?: unknown }).createObjectURL;
      delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("surfaces an error when the download fails", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/download")) {
        return Promise.resolve(jsonResponse({ detail: "backup not found" }, 404));
      }
      if (url === "/api/system/backup") return Promise.resolve(jsonResponse(listWithOne));
      if (url === "/api/settings") return Promise.resolve(jsonResponse(sampleSettings));
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^download$/i }));

    expect(await screen.findByText("backup not found")).toBeInTheDocument();
  });

  // --- Delete action (COL-65) --------------------------------------------------

  it("deletes a backup only after confirming, then refreshes the list", async () => {
    let deleted = false;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === `/api/system/backup/${sampleBackup.id}` && method === "DELETE") {
        deleted = true;
        return Promise.resolve({ ok: true, status: 204, json: () => Promise.resolve(null) });
      }
      if (url === "/api/system/backup") {
        return Promise.resolve(jsonResponse(deleted ? emptyList : listWithOne));
      }
      if (url === "/api/settings") return Promise.resolve(jsonResponse(sampleSettings));
      return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackupsPage />);

    // Clicking Delete asks for confirmation first -- it must not call DELETE yet.
    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
    expect(screen.getByText(/delete this backup\?/i)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, i]) => (i as RequestInit | undefined)?.method === "DELETE"),
    ).toBe(false);

    // Confirming performs the DELETE and the row drops out on the refreshed list.
    fireEvent.click(screen.getByRole("button", { name: /confirm delete/i }));

    expect(await screen.findByText(/no backups yet/i)).toBeInTheDocument();
    const deleteCall = fetchMock.mock.calls.find(
      ([, i]) => (i as RequestInit | undefined)?.method === "DELETE",
    );
    expect(deleteCall?.[0]).toBe(`/api/system/backup/${sampleBackup.id}`);
  });

  it("cancels a pending delete without calling DELETE", async () => {
    const fetchMock = stubFetch({ backups: listWithOne });
    render(<BackupsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    // Back to the plain Delete button, no confirmation prompt, no DELETE call.
    expect(screen.queryByText(/delete this backup\?/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, i]) => (i as RequestInit | undefined)?.method === "DELETE"),
    ).toBe(false);
  });

  it("surfaces the minimum-keep floor refusal (409) when a delete is rejected", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === `/api/system/backup/${sampleBackup.id}` && method === "DELETE") {
        return Promise.resolve(
          jsonResponse({ detail: "Refusing to delete this backup: at least 1 backup must be kept." }, 409),
        );
      }
      if (url === "/api/system/backup") return Promise.resolve(jsonResponse(listWithOne));
      if (url === "/api/settings") return Promise.resolve(jsonResponse(sampleSettings));
      return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^delete$/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm delete/i }));

    expect(await screen.findByText(/at least 1 backup must be kept/i)).toBeInTheDocument();
    // The backup is still listed -- the refusal didn't remove it.
    expect(screen.getByText(sampleBackup.name)).toBeInTheDocument();
  });
});
