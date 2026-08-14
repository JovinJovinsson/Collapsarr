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

/**
 * Default `GET /api/settings` body for tests that don't care about the
 * Auto-Queuing Pause (COL-181, COL-174) or Auto-Processing Pause (COL-226)
 * toggles -- every helper below routes `/api/settings` here unless a test
 * overrides it, so neither toggle's mount-time load has to be threaded
 * through every queue-only test.
 */
const DEFAULT_SETTINGS_RESPONSE = { auto_queue_paused: false, auto_processing_paused: false };

function mockFetch(handler: (url: string, init?: RequestInit) => { ok: boolean; status?: number; body: unknown }) {
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const { ok, status = 200, body } = handler(url, init);
    return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * Routes `GET /api/jobs/queue` through the call-indexed `responses` sequence
 * and `GET /api/settings` to `settings` (default {@link DEFAULT_SETTINGS_RESPONSE}).
 */
function mockFetchQueue(responses: unknown[], settings: unknown = DEFAULT_SETTINGS_RESPONSE) {
  let call = 0;
  return mockFetch((url) => {
    if (typeof url === "string" && url.includes("/api/settings")) {
      return { ok: true, body: settings };
    }
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
 * {@link mockFetchQueue} uses, `GET /api/settings` to `settings` (same
 * default as {@link mockFetchQueue}), and everything else -- the per-row
 * action endpoints, `POST /api/jobs/clear` (COL-181's "Clear queue"), and
 * `PUT /api/settings` (COL-181's "Pause auto-queuing" write) -- through
 * `actionHandler`, which inspects the URL/method itself. Lets a single test
 * drive the background poll and the settings load alongside a button click
 * against different responses.
 */
function mockFetchWithAction(
  queueResponses: unknown[],
  actionHandler: (url: string, init?: RequestInit) => { ok: boolean; status?: number; body: unknown },
  settings: unknown = DEFAULT_SETTINGS_RESPONSE,
) {
  let call = 0;
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (typeof url === "string" && url.includes("/api/jobs/queue")) {
      const body = queueResponses[Math.min(call, queueResponses.length - 1)];
      call += 1;
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    }
    if (
      typeof url === "string" &&
      url.includes("/api/settings") &&
      (!init?.method || init.method === "GET")
    ) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(settings) });
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
    it("shows \"Process next\" only on pending rows, not running rows (COL-193: Bump stays pending-only)", async () => {
      mockFetchQueue([queueResponse]);
      render(<QueuePage />);

      const runningRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      expect(within(runningRow).queryByRole("button", { name: /process next/i })).not.toBeInTheDocument();

      const pendingRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
      expect(within(pendingRow).getByRole("button", { name: /process next/i })).toBeInTheDocument();
    });

    it("shows \"Cancel\" on both pending and running rows (COL-193)", async () => {
      mockFetchQueue([queueResponse]);
      render(<QueuePage />);

      const runningRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      expect(within(runningRow).getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();

      const pendingRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
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

      // Mount: queue poll + settings load; then the bump POST + refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
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
      // Mount: queue poll + settings load; then the bump POST + refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
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

      // Mount: queue poll + settings load; then the DELETE + refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
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
      // Mount: queue poll + settings load; then the DELETE + refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
      // Still there, not stuck -- the row survives (it never left `pending` server-side).
      expect(screen.getByText("Show.S01E01")).toBeInTheDocument();
    });

    it("\"Cancel\" on a running row calls the DELETE endpoint for that job, and the row updates once the hard-kill lands (COL-193)", async () => {
      // The hard-killed Job transitions to `failed` (COL-192) and drops out
      // of the live queue (`GET /api/jobs/queue` only ever returns
      // pending/running rows) -- so, like the pending-row case, success is
      // observed as the row disappearing on refresh.
      const afterCancel = [pendingJobLowerPriority, pendingJobHigherPriority];
      const fetchMock = mockFetchWithAction([queueResponse, afterCancel], (url, init) => {
        expect(url).toContain(`/api/jobs/${runningJob.job_id}`);
        expect(url).not.toContain("bump");
        expect(init?.method).toBe("DELETE");
        return { ok: true, body: { cancelled: true } };
      });
      render(<QueuePage />);

      const runningRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      fireEvent.click(within(runningRow).getByRole("button", { name: /^cancel$/i }));

      // Mount: queue poll + settings load; then the DELETE + refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
      await waitFor(() => expect(screen.queryByText("Interstellar")).not.toBeInTheDocument());
    });

    it("\"Cancel\" on a running row shows a graceful notice, not an error, when the job finished first (too-late race, COL-193)", async () => {
      const fetchMock = mockFetchWithAction([queueResponse, queueResponse], () => ({
        ok: true,
        body: { cancelled: false },
      }));
      render(<QueuePage />);

      const runningRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      fireEvent.click(within(runningRow).getByRole("button", { name: /^cancel$/i }));

      expect(await screen.findByText(/already finished/i)).toBeInTheDocument();
      // Mount: queue poll + settings load; then the DELETE + refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
      // Still there, not stuck -- the row survives (it never left `running` server-side).
      expect(screen.getByText("Interstellar")).toBeInTheDocument();
      expect(within(runningRow).getByRole("button", { name: /^cancel$/i })).not.toBeDisabled();
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
      // Mount: queue poll + settings load; then the failed DELETE + refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    });
  });

  describe("polling", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    it("re-polls every ~5s while the queue has pending/running rows", async () => {
      const fetchMock = mockFetchQueue([queueResponse, queueResponse]);
      render(<QueuePage />);

      // +1 vs. the queue call alone: the one-shot Auto-Queuing Pause settings
      // load (COL-181) also fires on mount.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it("slows polling once the queue is empty, instead of continuing at the active 5s cadence", async () => {
      const fetchMock = mockFetchQueue([[]]);
      render(<QueuePage />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);

      // Not due yet at the active cadence.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);

      // Due at the slower idle cadence.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it("resumes the active 5s cadence once new work appears after an idle poll", async () => {
      const fetchMock = mockFetchQueue([[], queueResponse]);
      render(<QueuePage />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);

      // First poll was empty -> idle cadence (20s) brings the second poll,
      // which returns non-empty content.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(3);
      // Fake timers are active -- `findBy*`'s internal polling relies on real
      // timers, so assert synchronously; the state update above already
      // landed inside the `act` call.
      expect(screen.getByText("Interstellar")).toBeInTheDocument();

      // Back to the active 5s cadence for the third poll.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(4);
    });

    it("stops polling once the component unmounts", async () => {
      const fetchMock = mockFetchQueue([queueResponse, queueResponse, queueResponse]);
      const { unmount } = render(<QueuePage />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);

      unmount();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
  });

  describe('"Clear queue" (COL-181)', () => {
    it('shows "Clear queue" when the queue has a pending job', async () => {
      mockFetchQueue([queueResponse]);
      render(<QueuePage />);

      expect(await screen.findByRole("button", { name: /clear queue/i })).toBeInTheDocument();
    });

    it('hides "Clear queue" when there is no pending job', async () => {
      mockFetchQueue([[runningJob]]);
      render(<QueuePage />);

      await screen.findByText("Interstellar");
      expect(screen.queryByRole("button", { name: /clear queue/i })).not.toBeInTheDocument();
    });

    it("shows an inline confirm step before calling the bulk cancel endpoint", async () => {
      const fetchMock = mockFetchQueue([queueResponse]);
      render(<QueuePage />);

      fireEvent.click(await screen.findByRole("button", { name: /clear queue/i }));

      const confirmPanel = (await screen.findByText(/clear the queue\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      expect(
        within(confirmPanel).getByRole("button", { name: /confirm clear queue/i }),
      ).toBeInTheDocument();
      // Only the mount-time queue poll + settings load have happened so far --
      // the bulk cancel endpoint itself hasn't been called yet.
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('dismisses the confirm step via "Cancel" without calling the endpoint', async () => {
      const fetchMock = mockFetchQueue([queueResponse]);
      render(<QueuePage />);

      fireEvent.click(await screen.findByRole("button", { name: /clear queue/i }));
      const confirmPanel = (await screen.findByText(/clear the queue\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;

      fireEvent.click(within(confirmPanel).getByRole("button", { name: /^cancel$/i }));

      expect(screen.queryByText(/clear the queue\?/i)).not.toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it("calls POST /api/jobs/clear on confirm and reports the cancelled/already_running split", async () => {
      const fetchMock = mockFetchWithAction([queueResponse, []], (url, init) => {
        expect(url).toContain("/api/jobs/clear");
        expect(init?.method).toBe("POST");
        return { ok: true, body: { cancelled: 2, already_running: 1 } };
      });
      render(<QueuePage />);

      fireEvent.click(await screen.findByRole("button", { name: /clear queue/i }));
      const confirmPanel = (await screen.findByText(/clear the queue\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      fireEvent.click(within(confirmPanel).getByRole("button", { name: /confirm clear queue/i }));

      expect(
        await screen.findByText(
          /cancelled 2 pending jobs\. 1 job had already progressed past pending and could not be cancelled\./i,
        ),
      ).toBeInTheDocument();
      // The confirm step is dismissed once the endpoint call succeeds.
      expect(screen.queryByText(/clear the queue\?/i)).not.toBeInTheDocument();
      // Mount (queue poll + settings load) + the clear POST + the post-clear refresh queue poll.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    });

    it("reports a plain cancelled-count message when nothing was already running", async () => {
      mockFetchWithAction([queueResponse, []], () => ({
        ok: true,
        body: { cancelled: 3, already_running: 0 },
      }));
      render(<QueuePage />);

      fireEvent.click(await screen.findByRole("button", { name: /clear queue/i }));
      const confirmPanel = (await screen.findByText(/clear the queue\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      fireEvent.click(within(confirmPanel).getByRole("button", { name: /confirm clear queue/i }));

      expect(await screen.findByText(/^cancelled 3 pending jobs\.$/i)).toBeInTheDocument();
    });

    it("keeps the confirm step open and shows an error notice when the clear request fails", async () => {
      mockFetchWithAction([queueResponse, queueResponse], () => ({
        ok: false,
        status: 500,
        body: { detail: "boom" },
      }));
      render(<QueuePage />);

      fireEvent.click(await screen.findByRole("button", { name: /clear queue/i }));
      const confirmPanel = (await screen.findByText(/clear the queue\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      fireEvent.click(within(confirmPanel).getByRole("button", { name: /confirm clear queue/i }));

      expect(await screen.findByText(/boom/i)).toBeInTheDocument();
      // Still open for a retry, unlike the success path above.
      expect(screen.getByText(/clear the queue\?/i)).toBeInTheDocument();
    });
  });

  describe('"Pause auto-queuing" toggle (COL-181)', () => {
    it("reflects the active state on load", async () => {
      mockFetchQueue([queueResponse], { auto_queue_paused: false });
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-queuing: active/i });
      expect(toggle).toHaveAttribute("aria-pressed", "false");
    });

    it("reflects the paused state on load", async () => {
      mockFetchQueue([queueResponse], { auto_queue_paused: true });
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-queuing: paused/i });
      expect(toggle).toHaveAttribute("aria-pressed", "true");
    });

    it("flips the setting via PUT /api/settings when clicked, and reflects the response", async () => {
      mockFetchWithAction(
        [queueResponse],
        (url, init) => {
          expect(url).toContain("/api/settings");
          expect(init?.method).toBe("PUT");
          expect(JSON.parse(init?.body as string)).toEqual({ auto_queue_paused: true });
          return { ok: true, body: { auto_queue_paused: true } };
        },
        { auto_queue_paused: false },
      );
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-queuing: active/i });
      fireEvent.click(toggle);

      expect(await screen.findByRole("button", { name: /auto-queuing: paused/i })).toBeInTheDocument();
    });

    it("surfaces a page-level error notice and keeps the prior state when the write fails", async () => {
      mockFetchWithAction(
        [queueResponse],
        () => ({ ok: false, status: 500, body: { detail: "settings boom" } }),
        { auto_queue_paused: false },
      );
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-queuing: active/i });
      fireEvent.click(toggle);

      expect(await screen.findByText(/settings boom/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /auto-queuing: active/i })).toBeInTheDocument();
    });
  });

  describe('"Pause auto-processing" toggle (COL-226)', () => {
    it("reflects the active state on load", async () => {
      mockFetchQueue([queueResponse], { auto_queue_paused: false, auto_processing_paused: false });
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-processing: active/i });
      expect(toggle).toHaveAttribute("aria-pressed", "false");
    });

    it("reflects the paused state on load", async () => {
      mockFetchQueue([queueResponse], { auto_queue_paused: false, auto_processing_paused: true });
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-processing: paused/i });
      expect(toggle).toHaveAttribute("aria-pressed", "true");
    });

    it("flips the setting via PUT /api/settings when clicked, and reflects the response", async () => {
      mockFetchWithAction(
        [queueResponse],
        (url, init) => {
          expect(url).toContain("/api/settings");
          expect(init?.method).toBe("PUT");
          expect(JSON.parse(init?.body as string)).toEqual({ auto_processing_paused: true });
          return { ok: true, body: { auto_processing_paused: true } };
        },
        { auto_queue_paused: false, auto_processing_paused: false },
      );
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-processing: active/i });
      fireEvent.click(toggle);

      expect(
        await screen.findByRole("button", { name: /auto-processing: paused/i }),
      ).toBeInTheDocument();
    });

    it("surfaces a page-level error notice and keeps the prior state when the write fails", async () => {
      mockFetchWithAction(
        [queueResponse],
        () => ({ ok: false, status: 500, body: { detail: "processing settings boom" } }),
        { auto_queue_paused: false, auto_processing_paused: false },
      );
      render(<QueuePage />);

      const toggle = await screen.findByRole("button", { name: /auto-processing: active/i });
      fireEvent.click(toggle);

      expect(await screen.findByText(/processing settings boom/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /auto-processing: active/i })).toBeInTheDocument();
    });

    it("does not affect the independent Auto-Queuing Pause toggle", async () => {
      mockFetchWithAction(
        [queueResponse],
        () => ({ ok: true, body: { auto_processing_paused: true, auto_queue_paused: false } }),
        { auto_queue_paused: false, auto_processing_paused: false },
      );
      render(<QueuePage />);

      const processingToggle = await screen.findByRole("button", {
        name: /auto-processing: active/i,
      });
      fireEvent.click(processingToggle);

      expect(
        await screen.findByRole("button", { name: /auto-processing: paused/i }),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /auto-queuing: active/i })).toBeInTheDocument();
    });
  });
});
