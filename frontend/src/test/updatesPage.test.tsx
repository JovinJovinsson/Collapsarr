import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UpdatesPage } from "../pages/UpdatesPage";
import type { UpdateCheckState } from "../types/updates";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const upToDate: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.2.3",
  latest_version_label: "v1.2.3",
  changelog: null,
  checked_at: "2026-08-02T10:00:00Z",
  update_available: false,
};

const updateAvailable: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.3.0",
  latest_version_label: "Collapsarr v1.3.0",
  changelog: "- added things\n- fixed things",
  checked_at: "2026-08-02T10:00:00Z",
  update_available: true,
};

const neverChecked: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: null,
  latest_version_label: null,
  changelog: null,
  checked_at: null,
  update_available: true,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UpdatesPage", () => {
  it("shows a loading state, then the up-to-date state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(upToDate)));
    render(<UpdatesPage />);

    expect(screen.getByText(/loading update status/i)).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());
    expect(screen.getByText("v1.2.3")).toBeInTheDocument();
    expect(screen.getByText(/you're up to date/i)).toBeInTheDocument();
  });

  it("shows the update-available state, including the changelog", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(updateAvailable)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    expect(screen.getByText(/an update is available/i)).toHaveTextContent("v1.3.0");
    expect(screen.getAllByText("v1.3.0").length).toBeGreaterThan(0);
    expect(screen.getByText("Collapsarr v1.3.0")).toBeInTheDocument();
    expect(screen.getByText(/added things/)).toBeInTheDocument();
  });

  it("handles a never-checked state without a latest version", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(neverChecked)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    // "Latest version" row falls back to an em dash when null.
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThan(0);
  });

  it("shows an error state when the fetch fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<UpdatesPage />);

    await waitFor(() =>
      expect(screen.getByText(/couldn.t load update status: network down/i)).toBeInTheDocument()
    );
  });

  it("shows a Check now button that refreshes the state without a full page reload", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(upToDate))
      .mockResolvedValueOnce(jsonResponse(updateAvailable));
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Check now" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/system/updates/recheck",
      expect.objectContaining({ method: "POST" })
    );
    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    // Exactly the recheck round-trip -- no extra GET afterwards, since the
    // recheck response already carries the full, fresh state.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows an error banner when a recheck fails, leaving the stale state in place", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(upToDate))
      .mockResolvedValueOnce(jsonResponse({ detail: "scheduler unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Check now" }));

    await waitFor(() => expect(screen.getByText(/scheduler unavailable/i)).toBeInTheDocument());
    expect(screen.getByText(/you're up to date/i)).toBeInTheDocument();
  });
});
