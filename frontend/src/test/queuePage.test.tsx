import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueuePage } from "../pages/QueuePage";
import type { JobHistoryEntry } from "../types/activity";

const runningJob: JobHistoryEntry = {
  id: 1,
  job_id: "11111111-1111-1111-1111-111111111111",
  file_path: "/media/movies/Interstellar (2014)/Interstellar.mkv",
  status: "running",
  kind: "downmix",
  priority: 3,
  started_at: "2026-08-11T10:00:00Z",
  ended_at: null,
  exit_code: null,
  error_text: null,
  target: "5.1",
  language: "en",
  created_at: "2026-08-11T09:55:00Z",
  updated_at: "2026-08-11T10:00:00Z",
};

const pendingJobLowerPriority: JobHistoryEntry = {
  id: 2,
  job_id: "22222222-2222-2222-2222-222222222222",
  file_path: "/media/tv/Show/Season 01/Show.S01E01.mkv",
  status: "pending",
  kind: "set_default_audio",
  priority: 1,
  started_at: null,
  ended_at: null,
  exit_code: null,
  error_text: null,
  target: "stereo",
  language: "fr",
  created_at: "2026-08-11T09:56:00Z",
  updated_at: "2026-08-11T09:56:00Z",
};

const pendingJobHigherPriority: JobHistoryEntry = {
  id: 3,
  job_id: "33333333-3333-3333-3333-333333333333",
  file_path: "/media/tv/Other/Other.S01E02.mkv",
  status: "pending",
  kind: "downmix",
  priority: 2,
  started_at: null,
  ended_at: null,
  exit_code: null,
  error_text: null,
  target: "5.1",
  language: "en",
  created_at: "2026-08-11T09:57:00Z",
  updated_at: "2026-08-11T09:57:00Z",
};

/** Running-first, then pending ordered ascending by priority -- matches `GET /api/jobs/queue`'s (COL-175) contract. */
const queueResponse: JobHistoryEntry[] = [runningJob, pendingJobLowerPriority, pendingJobHigherPriority];

function mockFetch(handler: () => { ok: boolean; status?: number; body: unknown }) {
  const fetchMock = vi.fn().mockImplementation(() => {
    const { ok, status = 200, body } = handler();
    return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function mockFetchQueue(responses: unknown[]) {
  let call = 0;
  return mockFetch(() => {
    const body = responses[Math.min(call, responses.length - 1)];
    call += 1;
    return { ok: true, body };
  });
}

function mockFetchRejected(error: Error) {
  const fetchMock = vi.fn().mockRejectedValue(error);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * Routes `GET /api/jobs/queue` through the same call-indexed sequence
 * {@link mockFetchQueue} uses, and everything else (the per-row action
 * endpoints) through `actionHandler`, which inspects the URL/method itself
 * -- lets a single test drive both the background poll and a button click
 * against different responses.
 */
function mockFetchWithAction(
  queueResponses: unknown[],
  actionHandler: (url: string, init?: RequestInit) => { ok: boolean; status?: number; body: unknown },
) {
  let call = 0;
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (typeof url === "string" && url.includes("/api/jobs/queue")) {
      const body = queueResponses[Math.min(call, queueResponses.length - 1)];
      call += 1;
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    }
    const { ok, status = 200, body } = actionHandler(url, init);
    return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("QueuePage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("requests the live queue endpoint, not job history", async () => {
    const fetchMock = mockFetchQueue([queueResponse]);
    render(<QueuePage />);

    await screen.findByText("Interstellar");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/jobs/queue"),
      expect.anything(),
    );
    expect(fetchMock.mock.calls[0][0]).not.toContain("/api/jobs/history");
  });

  it("lists running jobs first, then pending jobs in the order the server returns them (ascending priority)", async () => {
    mockFetchQueue([queueResponse]);
    render(<QueuePage />);

    await screen.findByText("Interstellar");

    const titles = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.querySelector(".activity-table__title")?.textContent);
    // runningJob (running) first, then pendingJobLowerPriority (priority 1)
    // before pendingJobHigherPriority (priority 2) -- server order preserved,
    // no client-side re-sort.
    expect(titles).toEqual(["Interstellar", "Show.S01E01", "Other.S01E02"]);
  });

  it("shows status, target, and language for each row", async () => {
    mockFetchQueue([queueResponse]);
    render(<QueuePage />);

    const runningRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
    expect(within(runningRow).getByText("Running")).toBeInTheDocument();
    expect(within(runningRow).getByText("5.1")).toBeInTheDocument();
    expect(within(runningRow).getByText("en")).toBeInTheDocument();

    const pendingRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
    expect(within(pendingRow).getByText("Pending")).toBeInTheDocument();
    expect(within(pendingRow).getByText("stereo")).toBeInTheDocument();
    expect(within(pendingRow).getByText("fr")).toBeInTheDocument();
  });

  it("shows each row's job kind", async () => {
    mockFetchQueue([queueResponse]);
    render(<QueuePage />);

    const runningRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
    expect(within(runningRow).getByText("Downmix")).toBeInTheDocument();

    const pendingRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
    expect(within(pendingRow).getByText("Set Default Audio Track")).toBeInTheDocument();
  });

  it("filters by file path (case-insensitive substring)", async () => {
    mockFetchQueue([queueResponse]);
    render(<QueuePage />);

    await screen.findByText("Interstellar");

    fireEvent.change(screen.getByLabelText(/filter by file path/i), {
      target: { value: "show" },
    });

    expect(screen.getByText("Show.S01E01")).toBeInTheDocument();
    expect(screen.queryByText("Interstellar")).not.toBeInTheDocument();
    expect(screen.queryByText("Other.S01E02")).not.toBeInTheDocument();
  });

  it("shows a dedicated message when the filter excludes every row", async () => {
    mockFetchQueue([queueResponse]);
    render(<QueuePage />);

    await screen.findByText("Interstellar");

    fireEvent.change(screen.getByLabelText(/filter by file path/i), {
      target: { value: "nonexistent-file" },
    });

    expect(await screen.findByText(/no queued jobs match the current filter/i)).toBeInTheDocument();
  });

  it("does not show a status filter (every row is already pending/running)", async () => {
    mockFetchQueue([queueResponse]);
    render(<QueuePage />);

    await screen.findByText("Interstellar");
    expect(screen.queryByLabelText(/filter by status/i)).not.toBeInTheDocument();
  });

  it("renders a sensible empty state when the queue is empty", async () => {
    mockFetchQueue([[]]);
    render(<QueuePage />);

    expect(await screen.findByText(/queue is empty/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders an error state when the request fails", async () => {
    mockFetchRejected(new Error("network down"));
    render(<QueuePage />);

    expect(await screen.findByText(/couldn't load queue: network down/i)).toBeInTheDocument();
  });

  it("renders an error state on a non-ok response", async () => {
    mockFetch(() => ({ ok: false, status: 500, body: {} }));
    render(<QueuePage />);

    expect(await screen.findByText(/failed to load queue \(500\)/i)).toBeInTheDocument();
  });

  describe("per-row actions (COL-180)", () => {
    it("shows \"Process next\" and \"Cancel\" only on pending rows, not running rows", async () => {
      mockFetchQueue([queueResponse]);
      render(<QueuePage />);

      const runningRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      expect(within(runningRow).queryByRole("button", { name: /process next/i })).not.toBeInTheDocument();
      expect(within(runningRow).queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument();

      const pendingRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
      expect(within(pendingRow).getByRole("button", { name: /process next/i })).toBeInTheDocument();
      expect(within(pendingRow).getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
    });

    it("\"Process next\" calls the bump endpoint for that job and refreshes the queue on success", async () => {
      const refreshedQueue = [pendingJobHigherPriority, runningJob, pendingJobLowerPriority];
      const fetchMock = mockFetchWithAction([queueResponse, refreshedQueue], (url, init) => {
        expect(url).toContain(`/api/jobs/${pendingJobHigherPriority.job_id}/bump`);
        expect(init?.method).toBe("POST");
        return { ok: true, body: { bumped: true } };
      });
      render(<QueuePage />);

      const pendingRow = (await screen.findByText("Other.S01E02")).closest("tr") as HTMLElement;
      fireEvent.click(within(pendingRow).getByRole("button", { name: /process next/i }));

      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      const titles = await screen.findAllByRole("row");
      const firstDataRowTitle = titles[1].querySelector(".activity-table__title")?.textContent;
      expect(firstDataRowTitle).toBe("Other.S01E02");
    });

    it("\"Process next\" shows a graceful notice, not a stuck row, when the job started running first (too-late race)", async () => {
      const fetchMock = mockFetchWithAction([queueResponse, queueResponse], () => ({
        ok: true,
        body: { bumped: false },
      }));
      render(<QueuePage />);

      const pendingRow = (await screen.findByText("Other.S01E02")).closest("tr") as HTMLElement;
      fireEvent.click(within(pendingRow).getByRole("button", { name: /process next/i }));

      expect(await screen.findByText(/already started running/i)).toBeInTheDocument();
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      // The row is still there, not stuck mid-action -- the button is back to its normal label.
      expect(within(pendingRow).getByRole("button", { name: /process next/i })).not.toBeDisabled();
    });

    it("\"Cancel\" calls the DELETE endpoint for that job, and the row disappears once cancelled", async () => {
      const afterCancel = [runningJob, pendingJobHigherPriority];
      const fetchMock = mockFetchWithAction([queueResponse, afterCancel], (url, init) => {
        expect(url).toContain(`/api/jobs/${pendingJobLowerPriority.job_id}`);
        expect(url).not.toContain("bump");
        expect(init?.method).toBe("DELETE");
        return { ok: true, body: { cancelled: true } };
      });
      render(<QueuePage />);

      const pendingRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      fireEvent.click(within(pendingRow).getByRole("button", { name: /^cancel$/i }));

      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      await waitFor(() => expect(screen.queryByText("Show.S01E01")).not.toBeInTheDocument());
    });

    it("\"Cancel\" shows a graceful notice, not a stuck row, when the job started running first (too-late race)", async () => {
      const fetchMock = mockFetchWithAction([queueResponse, queueResponse], () => ({
        ok: true,
        body: { cancelled: false },
      }));
      render(<QueuePage />);

      const pendingRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      fireEvent.click(within(pendingRow).getByRole("button", { name: /^cancel$/i }));

      expect(await screen.findByText(/already started running/i)).toBeInTheDocument();
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      // Still there, not stuck -- the row survives (it never left `pending` server-side).
      expect(screen.getByText("Show.S01E01")).toBeInTheDocument();
    });

    it("surfaces a clear message, not a stuck row, when the cancel target is already gone (404)", async () => {
      const fetchMock = mockFetchWithAction([queueResponse, queueResponse], () => ({
        ok: false,
        status: 404,
        body: { detail: "No such job: 22222222-2222-2222-2222-222222222222" },
      }));
      render(<QueuePage />);

      const pendingRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      fireEvent.click(within(pendingRow).getByRole("button", { name: /^cancel$/i }));

      expect(await screen.findByText(/no such job/i)).toBeInTheDocument();
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    });
  });

  describe("polling", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    it("re-polls every ~5s while the queue has pending/running rows", async () => {
      const fetchMock = mockFetchQueue([queueResponse, queueResponse]);
      render(<QueuePage />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it("slows polling once the queue is empty, instead of continuing at the active 5s cadence", async () => {
      const fetchMock = mockFetchQueue([[]]);
      render(<QueuePage />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);

      // Not due yet at the active cadence.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);

      // Due at the slower idle cadence.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it("resumes the active 5s cadence once new work appears after an idle poll", async () => {
      const fetchMock = mockFetchQueue([[], queueResponse]);
      render(<QueuePage />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);

      // First poll was empty -> idle cadence (20s) brings the second poll,
      // which returns non-empty content.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      // Fake timers are active -- `findBy*`'s internal polling relies on real
      // timers, so assert synchronously; the state update above already
      // landed inside the `act` call.
      expect(screen.getByText("Interstellar")).toBeInTheDocument();

      // Back to the active 5s cadence for the third poll.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it("stops polling once the component unmounts", async () => {
      const fetchMock = mockFetchQueue([queueResponse, queueResponse, queueResponse]);
      const { unmount } = render(<QueuePage />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);

      unmount();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });
});
