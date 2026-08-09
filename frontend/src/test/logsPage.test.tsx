import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LogsPage } from "../pages/LogsPage";
import type { LogsResponse } from "../types/logs";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const initialPage: LogsResponse = {
  entries: [
    { line_number: 1, level: "INFO", text: "2026-08-09 12:00:00,000 INFO collapsarr.app started" },
    { line_number: 2, level: "WARNING", text: "2026-08-09 12:00:01,000 WARNING collapsarr.app low disk" },
  ],
  next_offset: 2,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("LogsPage", () => {
  it("renders the current tail window on mount", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(initialPage));
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());
    expect(screen.getByText(/low disk/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/system/logs", expect.anything());
  });

  it("shows an error state when the initial load fails", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/Couldn't load logs/)).toBeInTheDocument());
  });

  it("shows an empty state when the file has no matching lines", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ entries: [], next_offset: null }));
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/log file is empty/)).toBeInTheDocument());
  });

  it("re-fetches with the selected minimum-severity level", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(initialPage)) // initial GET on mount
      .mockResolvedValueOnce(
        jsonResponse({
          entries: [initialPage.entries[1]],
          next_offset: null,
        })
      ); // GET after selecting WARNING
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/minimum severity/i), { target: { value: "WARNING" } });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/system/logs?level=WARNING", expect.anything());
    await waitFor(() => expect(screen.queryByText(/started/)).not.toBeInTheDocument());
    expect(screen.getByText(/low disk/)).toBeInTheDocument();
  });

  it("refresh re-fetches the current tail window", async () => {
    const refreshedPage: LogsResponse = {
      entries: [
        ...initialPage.entries,
        { line_number: 3, level: "ERROR", text: "2026-08-09 12:00:02,000 ERROR collapsarr.app boom" },
      ],
      next_offset: 3,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(initialPage))
      .mockResolvedValueOnce(jsonResponse(refreshedPage));
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/system/logs", expect.anything());
  });

  it("load older pages further back and prepends the older entries", async () => {
    const olderPage: LogsResponse = {
      entries: [
        { line_number: -1, level: "DEBUG", text: "2026-08-09 11:59:59,000 DEBUG collapsarr.app boot" },
      ],
      next_offset: null,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(initialPage)) // initial GET
      .mockResolvedValueOnce(jsonResponse(olderPage)); // GET with offset
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Load older" }));

    await waitFor(() => expect(screen.getByText(/boot/)).toBeInTheDocument());
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/system/logs?offset=2", expect.anything());
    // Older entries are prepended, and the previously-shown lines remain.
    expect(screen.getByText(/started/)).toBeInTheDocument();
    expect(screen.getByText(/low disk/)).toBeInTheDocument();
    // No more to load -- the button now shows the disabled "No older lines" state.
    expect(screen.getByRole("button", { name: "No older lines" })).toBeDisabled();
  });
});
