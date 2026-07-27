import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BackupsPage } from "../pages/BackupsPage";
import type { Backup, BackupList } from "../types/backups";

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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BackupsPage", () => {
  it("renders the heading and existing backups from a mocked GET", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listWithOne)));
    render(<BackupsPage />);

    expect(await screen.findByRole("heading", { name: "Backups" })).toBeInTheDocument();
    expect(await screen.findByText(sampleBackup.name)).toBeInTheDocument();
    // Size rendered human-readable, type labelled.
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByText("Manual")).toBeInTheDocument();
  });

  it("shows an empty state and a Backup now button when there are no backups", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(emptyList)));
    render(<BackupsPage />);

    expect(await screen.findByText(/no backups yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /backup now/i })).toBeInTheDocument();
  });

  it("creates a backup via POST and shows it in the refreshed list", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST") {
        return Promise.resolve(jsonResponse(sampleBackup, 202));
      }
      // First GET: empty. After the POST, the reload GET returns the new backup.
      const call = fetchMock.mock.calls.filter(([, i]) => (i as RequestInit | undefined)?.method !== "POST").length;
      return Promise.resolve(jsonResponse(call <= 1 ? emptyList : listWithOne));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /backup now/i }));

    expect(await screen.findByText(sampleBackup.name)).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(([, i]) => (i as RequestInit | undefined)?.method === "POST");
    expect(postCall?.[0]).toBe("/api/system/backup");
  });

  it("surfaces an error when creating a backup fails", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST") {
        return Promise.resolve(jsonResponse({ detail: "disk full" }, 500));
      }
      return Promise.resolve(jsonResponse(emptyList));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<BackupsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /backup now/i }));

    expect(await screen.findByText("disk full")).toBeInTheDocument();
  });

  it("shows the unavailable state (no controls) when the DB isn't file-based SQLite", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ supported: false, backups: [] } satisfies BackupList)),
    );
    render(<BackupsPage />);

    expect(await screen.findByText(/unavailable for this database configuration/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /backup now/i })).not.toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<BackupsPage />);

    expect(await screen.findByText(/couldn't load backups: network down/i)).toBeInTheDocument();
  });

  it("displays the created timestamp in local time", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listWithOne)));
    render(<BackupsPage />);

    const row = (await screen.findByText(sampleBackup.name)).closest("tr");
    expect(row).not.toBeNull();
    const expected = new Date(sampleBackup.created_at).toLocaleString();
    expect(within(row as HTMLElement).getByText(expected)).toBeInTheDocument();
  });
});
