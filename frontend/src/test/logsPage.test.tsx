import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LogsPage } from "../pages/LogsPage";
import type { LogFileList, LogsResponse } from "../types/logs";

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

const emptyFileList: LogFileList = { files: [] };
const oneFileList: LogFileList = {
  files: [{ name: "collapsarr.log", size: 2048, modified_at: "2026-08-09T12:00:00Z" }],
};

/**
 * Routes a mocked `fetch` by URL/method: the tail-window `GET /api/system/logs`
 * vs. the file-listing `GET /api/system/logs/files` vs. `DELETE /api/system/logs`
 * vs. a per-file download. `LogsPage` fires the tail-window and file-listing
 * requests independently on mount (two separate effects), so tests route by
 * URL rather than relying on call order (mirrors `backupsPage.test.tsx`'s
 * `stubFetch` helper).
 */
function stubFetch(options: {
  logsPage?: LogsResponse;
  files?: LogFileList;
  onDelete?: () => { ok: boolean; status: number; json: () => Promise<unknown> };
  onDownload?: (name: string) => { ok: boolean; status: number; blob: () => Promise<Blob> };
}) {
  const { logsPage = initialPage, files = emptyFileList, onDelete, onDownload } = options;

  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/system/logs" && method === "DELETE") {
      return Promise.resolve(onDelete ? onDelete() : { ok: true, status: 204, json: () => Promise.resolve(null) });
    }
    const downloadMatch = /^\/api\/system\/logs\/files\/([^/]+)\/download$/.exec(url);
    if (downloadMatch && onDownload) {
      return Promise.resolve(onDownload(downloadMatch[1]));
    }
    if (url === "/api/system/logs/files") {
      return Promise.resolve(jsonResponse(files));
    }
    if (url.startsWith("/api/system/logs")) {
      return Promise.resolve(jsonResponse(logsPage));
    }
    return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("LogsPage", () => {
  it("renders the current tail window on mount", async () => {
    stubFetch({});

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());
    expect(screen.getByText(/low disk/)).toBeInTheDocument();
  });

  it("shows an error state when the initial load fails", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/system/logs/files") return Promise.resolve(jsonResponse(emptyFileList));
      return Promise.resolve(jsonResponse({ detail: "boom" }, 500));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/Couldn't load logs/)).toBeInTheDocument());
  });

  it("shows an empty state when the file has no matching lines", async () => {
    stubFetch({ logsPage: { entries: [], next_offset: null } });

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/log file is empty/)).toBeInTheDocument());
  });

  it("re-fetches with the selected minimum-severity level", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/system/logs/files") return Promise.resolve(jsonResponse(emptyFileList));
      if (url === "/api/system/logs?level=WARNING") {
        return Promise.resolve(jsonResponse({ entries: [initialPage.entries[1]], next_offset: null }));
      }
      if (url === "/api/system/logs") return Promise.resolve(jsonResponse(initialPage));
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/minimum severity/i), { target: { value: "WARNING" } });

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => url === "/api/system/logs?level=WARNING")).toBe(true)
    );
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
    let refreshed = false;
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/system/logs/files") return Promise.resolve(jsonResponse(emptyFileList));
      if (url === "/api/system/logs") {
        const page = refreshed ? refreshedPage : initialPage;
        refreshed = true;
        return Promise.resolve(jsonResponse(page));
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
  });

  it("load older pages further back and prepends the older entries", async () => {
    const olderPage: LogsResponse = {
      entries: [
        { line_number: -1, level: "DEBUG", text: "2026-08-09 11:59:59,000 DEBUG collapsarr.app boot" },
      ],
      next_offset: null,
    };
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/system/logs/files") return Promise.resolve(jsonResponse(emptyFileList));
      if (url === "/api/system/logs?offset=2") return Promise.resolve(jsonResponse(olderPage));
      if (url === "/api/system/logs") return Promise.resolve(jsonResponse(initialPage));
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Load older" }));

    await waitFor(() => expect(screen.getByText(/boot/)).toBeInTheDocument());
    // Older entries are prepended, and the previously-shown lines remain.
    expect(screen.getByText(/started/)).toBeInTheDocument();
    expect(screen.getByText(/low disk/)).toBeInTheDocument();
    // No more to load -- the button now shows the disabled "No older lines" state.
    expect(screen.getByRole("button", { name: "No older lines" })).toBeDisabled();
  });

  it("renders merged rows correctly even when a rotation reuses line_number across fetches", async () => {
    // COL-131 review: `line_number` is only unique within a single response -- the
    // backend re-reads the file fresh each request, so a rotation between "Refresh"/
    // "Load older" can reuse a low line_number for entirely different content. Simulate
    // that here: the "Load older" page reuses line_number 1 and 2, colliding with the
    // values already on screen from the initial fetch.
    const olderPageAfterRotation: LogsResponse = {
      entries: [
        {
          line_number: 1,
          level: "DEBUG",
          text: "2026-08-09 09:00:00,000 DEBUG collapsarr.app rotated-old-1",
        },
        {
          line_number: 2,
          level: "DEBUG",
          text: "2026-08-09 09:00:01,000 DEBUG collapsarr.app rotated-old-2",
        },
      ],
      next_offset: null,
    };
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/system/logs/files") return Promise.resolve(jsonResponse(emptyFileList));
      if (url === "/api/system/logs?offset=2") return Promise.resolve(jsonResponse(olderPageAfterRotation));
      if (url === "/api/system/logs") return Promise.resolve(jsonResponse(initialPage));
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(<LogsPage />);

    await waitFor(() => expect(screen.getByText(/started/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Load older" }));

    await waitFor(() => expect(screen.getByText(/rotated-old-2/)).toBeInTheDocument());

    // All four rows render distinctly despite colliding line_number values across fetches.
    expect(screen.getByText(/rotated-old-1/)).toBeInTheDocument();
    expect(screen.getByText(/started/)).toBeInTheDocument();
    expect(screen.getByText(/low disk/)).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(5); // header row + 4 entry rows

    // React logs a "unique key" warning via console.error when duplicate keys collide.
    const hasKeyWarning = consoleErrorSpy.mock.calls.some((args) =>
      args.some((arg) => typeof arg === "string" && arg.toLowerCase().includes("unique") && arg.toLowerCase().includes("key"))
    );
    expect(hasKeyWarning).toBe(false);

    consoleErrorSpy.mockRestore();
  });

  // --- Log files section (COL-132) ------------------------------------------

  it("renders the file list from a mocked GET /api/system/logs/files", async () => {
    stubFetch({ files: oneFileList });

    render(<LogsPage />);

    expect(await screen.findByText("collapsarr.log")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
  });

  it("shows an empty state when there are no log files", async () => {
    stubFetch({ files: emptyFileList });

    render(<LogsPage />);

    expect(await screen.findByText(/no log files yet/i)).toBeInTheDocument();
  });

  it("downloads a log file via the per-row Download action", async () => {
    const blob = new Blob(["fake log bytes"], { type: "text/plain" });
    const fetchMock = stubFetch({
      files: oneFileList,
      onDownload: () => ({ ok: true, status: 200, blob: () => Promise.resolve(blob) }),
    });

    const createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, configurable: true });
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

    try {
      render(<LogsPage />);
      fireEvent.click(await screen.findByRole("button", { name: /^download$/i }));

      await waitFor(() => expect(clickSpy).toHaveBeenCalledTimes(1));
      expect(createObjectURL).toHaveBeenCalledWith(blob);
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
      const downloadCall = fetchMock.mock.calls.find(([url]) => (url as string).endsWith("/download"));
      expect(downloadCall?.[0]).toBe("/api/system/logs/files/collapsarr.log/download");
    } finally {
      delete (URL as { createObjectURL?: unknown }).createObjectURL;
      delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
      clickSpy.mockRestore();
    }
  });

  it("clear logs requires confirmation before calling DELETE", async () => {
    const fetchMock = stubFetch({ files: oneFileList });

    render(<LogsPage />);
    await screen.findByText("collapsarr.log");

    fireEvent.click(screen.getByRole("button", { name: /clear logs/i }));

    expect(screen.getByText(/delete every log file/i)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "DELETE")
    ).toBe(false);
  });

  it("cancels a pending clear without calling DELETE", async () => {
    const fetchMock = stubFetch({ files: oneFileList });

    render(<LogsPage />);
    await screen.findByText("collapsarr.log");

    fireEvent.click(screen.getByRole("button", { name: /clear logs/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(screen.queryByText(/delete every log file/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /clear logs/i })).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "DELETE")
    ).toBe(false);
  });

  it("clears logs after confirming, then refreshes the file list and tail window", async () => {
    let cleared = false;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/system/logs" && method === "DELETE") {
        cleared = true;
        return Promise.resolve({ ok: true, status: 204, json: () => Promise.resolve(null) });
      }
      if (url === "/api/system/logs/files") {
        return Promise.resolve(jsonResponse(cleared ? emptyFileList : oneFileList));
      }
      if (url === "/api/system/logs") {
        return Promise.resolve(jsonResponse(cleared ? { entries: [], next_offset: null } : initialPage));
      }
      return Promise.reject(new Error(`Unexpected fetch: ${method} ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LogsPage />);
    await screen.findByText("collapsarr.log");
    await screen.findByText(/started/);

    fireEvent.click(screen.getByRole("button", { name: /clear logs/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm clear/i }));

    await waitFor(() => expect(screen.getByText(/no log files yet/i)).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/log file is empty/i)).toBeInTheDocument());
    const deleteCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === "DELETE"
    );
    expect(deleteCall?.[0]).toBe("/api/system/logs");
  });

  it("surfaces an error when clearing logs fails", async () => {
    const fetchMock = stubFetch({
      files: oneFileList,
      onDelete: () => jsonResponse({ detail: "disk error" }, 500),
    });

    render(<LogsPage />);
    await screen.findByText("collapsarr.log");

    fireEvent.click(screen.getByRole("button", { name: /clear logs/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm clear/i }));

    expect(await screen.findByText("disk error")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "DELETE")).toBe(
      true
    );
    // The file is still listed -- the failed clear didn't remove it optimistically.
    expect(screen.getByText("collapsarr.log")).toBeInTheDocument();
  });
});
