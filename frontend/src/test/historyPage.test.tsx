import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HistoryPage } from "../pages/HistoryPage";
import type { JobHistoryEntry } from "../types/activity";

const succeededJob: JobHistoryEntry = {
  id: 1,
  job_id: "11111111-1111-1111-1111-111111111111",
  file_path: "/media/movies/Interstellar (2014)/Interstellar.mkv",
  status: "succeeded",
  kind: "downmix",
  priority: 0,
  scheduled_at: null,
  started_at: "2026-07-10T10:00:00Z",
  ended_at: "2026-07-10T10:05:00Z",
  exit_code: 0,
  error_text: null,
  target: "5.1",
  language: "en",
  created_at: "2026-07-10T10:00:00Z",
  updated_at: "2026-07-10T10:05:00Z",
};

const failedJob: JobHistoryEntry = {
  id: 2,
  job_id: "22222222-2222-2222-2222-222222222222",
  file_path: "/media/tv/Show/Season 01/Show.S01E01.mkv",
  status: "failed",
  kind: "set_default_audio",
  priority: 0,
  scheduled_at: null,
  started_at: "2026-07-11T08:00:00Z",
  ended_at: "2026-07-11T08:01:00Z",
  exit_code: 1,
  error_text: "ffmpeg exited with an error",
  target: "stereo",
  language: "fr",
  created_at: "2026-07-11T08:00:00Z",
  updated_at: "2026-07-11T08:01:00Z",
};

/**
 * A still-live job mixed into the raw `/api/jobs/history` response -- proves
 * `HistoryPage` (COL-176) excludes pending/running rows entirely, unlike the
 * old combined `ActivityPage` which showed them. `QueuePage` (COL-178) owns
 * these.
 */
const pendingJob: JobHistoryEntry = {
  id: 3,
  job_id: "33333333-3333-3333-3333-333333333333",
  file_path: "/media/tv/Newest/Newest.S01E01.mkv",
  status: "pending",
  kind: "downmix",
  priority: 0,
  scheduled_at: null,
  started_at: null,
  ended_at: null,
  exit_code: null,
  error_text: null,
  target: null,
  language: null,
  created_at: "2026-07-12T00:00:00Z",
  updated_at: "2026-07-12T00:00:00Z",
};

const historyResponse: JobHistoryEntry[] = [succeededJob, failedJob, pendingJob];

function mockFetchResolved(body: unknown, ok = true, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok,
    status,
    json: () => Promise.resolve(body),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function mockFetchRejected(error: Error) {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
}

/**
 * Routes `GET /api/jobs/history` to `historyBody` and everything else (the
 * `POST /api/jobs/requeue` action) through `actionHandler`, which inspects
 * the URL/method itself. Mirrors `queuePage.test.tsx`'s `mockFetchWithAction`.
 */
function mockFetchWithAction(
  historyBody: unknown,
  actionHandler: (url: string, init?: RequestInit) => { ok: boolean; status?: number; body: unknown },
) {
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (typeof url === "string" && url.includes("/api/jobs/history")) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(historyBody) });
    }
    const { ok, status = 200, body } = actionHandler(url, init);
    return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("HistoryPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("requests job history, not the live queue", async () => {
    const fetchMock = mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    await screen.findByText("Interstellar");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/jobs/history"),
      expect.anything(),
    );
    expect(fetchMock.mock.calls[0][0]).not.toContain("/api/jobs/queue");
  });

  it("lists terminal job history with status, timestamps, exit code, target/language", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    const succeededTitle = await screen.findByText("Interstellar");
    const succeededRow = succeededTitle.closest("tr");
    expect(succeededRow).not.toBeNull();
    const scopedSucceeded = within(succeededRow as HTMLElement);
    expect(
      scopedSucceeded.getByText("/media/movies/Interstellar (2014)/Interstellar.mkv"),
    ).toBeInTheDocument();
    expect(scopedSucceeded.getByText("Succeeded")).toBeInTheDocument();
    expect(scopedSucceeded.getByText("0")).toBeInTheDocument();
    expect(scopedSucceeded.getByText("5.1")).toBeInTheDocument();
    expect(scopedSucceeded.getByText("en")).toBeInTheDocument();

    const failedTitle = screen.getByText("Show.S01E01");
    const failedRow = failedTitle.closest("tr");
    expect(failedRow).not.toBeNull();
    const scopedFailed = within(failedRow as HTMLElement);
    expect(scopedFailed.getByText("Failed")).toBeInTheDocument();
    expect(scopedFailed.getByText("1")).toBeInTheDocument();
    expect(scopedFailed.getByText("ffmpeg exited with an error")).toBeInTheDocument();
    expect(scopedFailed.getByText("stereo")).toBeInTheDocument();
    expect(scopedFailed.getByText("fr")).toBeInTheDocument();
  });

  it("excludes pending/running rows entirely, even though the raw history response includes one", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    await screen.findByText("Interstellar");
    expect(screen.queryByText("Newest.S01E01")).not.toBeInTheDocument();
  });

  it("shows each row's job kind", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    const succeededRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
    expect(within(succeededRow).getByText("Downmix")).toBeInTheDocument();

    const failedRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
    expect(within(failedRow).getByText("Set Default Audio Track")).toBeInTheDocument();
  });

  it("shows the most recently queued job at the top of the list", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    await screen.findByText("Interstellar");

    const titles = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.querySelector(".activity-table__title")?.textContent);
    expect(titles).toEqual(["Show.S01E01", "Interstellar"]);
  });

  it("filters by file path (case-insensitive substring)", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    await screen.findByText("Interstellar");

    fireEvent.change(screen.getByLabelText(/filter by file path/i), {
      target: { value: "interstellar" },
    });

    expect(screen.getByText("Interstellar")).toBeInTheDocument();
    expect(screen.queryByText("Show.S01E01")).not.toBeInTheDocument();
  });

  it("filters by status, offering only Succeeded/Failed (no Pending/Running options)", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    await screen.findByText("Interstellar");

    const statusSelect = screen.getByLabelText(/filter by status/i) as HTMLSelectElement;
    const optionLabels = Array.from(statusSelect.options).map((option) => option.textContent);
    expect(optionLabels).toEqual(["All statuses", "Succeeded", "Failed"]);

    fireEvent.change(statusSelect, { target: { value: "failed" } });

    expect(screen.queryByText("Interstellar")).not.toBeInTheDocument();
    expect(screen.getByText("Show.S01E01")).toBeInTheDocument();
  });

  it("shows a dedicated message when filters exclude every row", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    await screen.findByText("Interstellar");

    fireEvent.change(screen.getByLabelText(/filter by file path/i), {
      target: { value: "nonexistent-file" },
    });

    expect(await screen.findByText(/no job history matches the current filters/i)).toBeInTheDocument();
  });

  it("renders a sensible empty state when no terminal jobs have run", async () => {
    mockFetchResolved([]);
    render(<HistoryPage />);

    expect(await screen.findByText(/no history yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the empty state even when the raw history has only live (pending/running) rows", async () => {
    mockFetchResolved([pendingJob]);
    render(<HistoryPage />);

    expect(await screen.findByText(/no history yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders an error state when the request fails", async () => {
    mockFetchRejected(new Error("network down"));
    render(<HistoryPage />);

    expect(await screen.findByText(/couldn't load job history: network down/i)).toBeInTheDocument();
  });

  it("renders an error state on a non-ok response", async () => {
    mockFetchResolved({}, false, 500);
    render(<HistoryPage />);

    expect(await screen.findByText(/failed to load job history \(500\)/i)).toBeInTheDocument();
  });

  it("shows a short error inline with no 'Show more' action", async () => {
    mockFetchResolved(historyResponse);
    render(<HistoryPage />);

    const failedRow = (await screen.findByText("Show.S01E01")).closest("tr");
    expect(within(failedRow as HTMLElement).getByText("ffmpeg exited with an error")).toBeInTheDocument();
    expect(within(failedRow as HTMLElement).queryByText(/show more/i)).not.toBeInTheDocument();
  });

  it("truncates a huge error with a 'Show more' modal showing the full text", async () => {
    const hugeError = "ffmpeg stderr: ".repeat(20).trim();
    mockFetchResolved([
      { ...failedJob, id: 4, file_path: "/media/tv/Big/Big.S01E01.mkv", error_text: hugeError },
    ]);
    render(<HistoryPage />);

    const row = (await screen.findByText("Big.S01E01")).closest("tr");
    expect(within(row as HTMLElement).getByRole("button", { name: /show more/i })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: /show more/i }));

    const dialog = await screen.findByRole("dialog", { name: /big/i });
    expect(within(dialog).getByText(hugeError)).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  describe('"Requeue" action (COL-176, COL-170)', () => {
    it("shows Requeue only on failed rows, not succeeded rows", async () => {
      mockFetchResolved(historyResponse);
      render(<HistoryPage />);

      const succeededRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      expect(within(succeededRow).queryByRole("button", { name: /requeue/i })).not.toBeInTheDocument();

      const failedRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
      expect(within(failedRow).getByRole("button", { name: /requeue/i })).toBeInTheDocument();
    });

    it("requeues immediately on click with no confirm dialog, hitting the single-file requeue endpoint", async () => {
      const fetchMock = mockFetchWithAction(historyResponse, (url, init) => {
        expect(url).toContain("/api/jobs/requeue");
        expect(init?.method).toBe("POST");
        expect(JSON.parse(init?.body as string)).toEqual({ file_path: failedJob.file_path });
        return {
          ok: true,
          body: { enqueued: true, job: { id: "job-1", file_path: failedJob.file_path, status: "pending" } },
        };
      });
      render(<HistoryPage />);

      const failedRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      expect(screen.queryByText(/clear the queue\?/i)).not.toBeInTheDocument();
      fireEvent.click(within(failedRow).getByRole("button", { name: /requeue/i }));

      expect(await screen.findByText(/requeued "show\.s01e01" for processing/i)).toBeInTheDocument();
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    });

    it("disables the button while the requeue is in flight, then re-enables it", async () => {
      let resolveRequeue: (() => void) | undefined;
      const fetchMock = vi.fn().mockImplementation((url: string) => {
        if (typeof url === "string" && url.includes("/api/jobs/history")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(historyResponse),
          });
        }
        return new Promise((resolve) => {
          resolveRequeue = () =>
            resolve({
              ok: true,
              status: 200,
              json: () => Promise.resolve({ enqueued: true, job: null }),
            });
        });
      });
      vi.stubGlobal("fetch", fetchMock);
      render(<HistoryPage />);

      const failedRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      const button = within(failedRow).getByRole("button", { name: /requeue/i });
      fireEvent.click(button);

      expect(await within(failedRow).findByRole("button", { name: /requeuing/i })).toBeDisabled();

      resolveRequeue?.();

      expect(await within(failedRow).findByRole("button", { name: /^requeue$/i })).not.toBeDisabled();
    });

    it("shows a muted hint, not an error, when the file could not be requeued (enqueued: false)", async () => {
      mockFetchWithAction(historyResponse, () => ({
        ok: true,
        body: { enqueued: false, job: null },
      }));
      render(<HistoryPage />);

      const failedRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      fireEvent.click(within(failedRow).getByRole("button", { name: /requeue/i }));

      expect(await screen.findByText(/could not be requeued right now/i)).toBeInTheDocument();
    });

    it("shows an error notice when the requeue request fails", async () => {
      mockFetchWithAction(historyResponse, () => ({
        ok: false,
        status: 500,
        body: { detail: "boom" },
      }));
      render(<HistoryPage />);

      const failedRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      fireEvent.click(within(failedRow).getByRole("button", { name: /requeue/i }));

      expect(await screen.findByText(/boom/i)).toBeInTheDocument();
    });

    it("leaves the failed row exactly as it was after a successful requeue (no re-fetch)", async () => {
      const fetchMock = mockFetchWithAction(historyResponse, () => ({
        ok: true,
        body: { enqueued: true, job: { id: "job-1", file_path: failedJob.file_path, status: "pending" } },
      }));
      render(<HistoryPage />);

      const failedRow = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      fireEvent.click(within(failedRow).getByRole("button", { name: /requeue/i }));

      await screen.findByText(/requeued "show\.s01e01" for processing/i);
      // Only the initial history fetch + the requeue POST -- no follow-up fetch.
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(screen.getByText("Show.S01E01")).toBeInTheDocument();
      expect(within(failedRow).getByText("Failed")).toBeInTheDocument();
    });
  });

  describe('"Requeue all failed" action (COL-179, COL-172)', () => {
    it('shows "Requeue all failed" when at least one row is failed', async () => {
      mockFetchResolved(historyResponse);
      render(<HistoryPage />);

      expect(await screen.findByRole("button", { name: /requeue all failed/i })).toBeInTheDocument();
    });

    it('hides "Requeue all failed" when there is no failed row', async () => {
      mockFetchResolved([succeededJob]);
      render(<HistoryPage />);

      await screen.findByText("Interstellar");
      expect(screen.queryByRole("button", { name: /requeue all failed/i })).not.toBeInTheDocument();
    });

    it("shows an inline confirm step before calling the bulk requeue endpoint", async () => {
      const fetchMock = mockFetchResolved(historyResponse);
      render(<HistoryPage />);

      fireEvent.click(await screen.findByRole("button", { name: /requeue all failed/i }));

      const confirmPanel = (await screen.findByText(/requeue every failed job\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      expect(
        within(confirmPanel).getByRole("button", { name: /confirm requeue all failed/i }),
      ).toBeInTheDocument();
      // Only the mount-time history fetch has happened so far -- the bulk
      // requeue endpoint itself hasn't been called yet.
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it('dismisses the confirm step via "Cancel" without calling the endpoint', async () => {
      const fetchMock = mockFetchResolved(historyResponse);
      render(<HistoryPage />);

      fireEvent.click(await screen.findByRole("button", { name: /requeue all failed/i }));
      const confirmPanel = (await screen.findByText(/requeue every failed job\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;

      fireEvent.click(within(confirmPanel).getByRole("button", { name: /^cancel$/i }));

      expect(screen.queryByText(/requeue every failed job\?/i)).not.toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it("calls POST /api/jobs/requeue-failed on confirm, reports the requeued/skipped split, and re-fetches history", async () => {
      const fetchMock = mockFetchWithAction(historyResponse, (url, init) => {
        expect(url).toContain("/api/jobs/requeue-failed");
        expect(init?.method).toBe("POST");
        return {
          ok: true,
          body: {
            requeued: Array.from({ length: 12 }, (_, i) => ({
              id: `job-${i}`,
              file_path: `/media/${i}.mkv`,
              status: "pending",
            })),
            skipped: ["/media/a.mkv", "/media/b.mkv", "/media/c.mkv"],
          },
        };
      });
      render(<HistoryPage />);

      fireEvent.click(await screen.findByRole("button", { name: /requeue all failed/i }));
      const confirmPanel = (await screen.findByText(/requeue every failed job\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      fireEvent.click(within(confirmPanel).getByRole("button", { name: /confirm requeue all failed/i }));

      expect(
        await screen.findByText(
          /12 of 15 requeued, 3 skipped — inside the deduplication window, check logs and requeue individually\./i,
        ),
      ).toBeInTheDocument();
      // The confirm step is dismissed once the endpoint call succeeds.
      expect(screen.queryByText(/requeue every failed job\?/i)).not.toBeInTheDocument();
      // Initial history fetch + the requeue-failed POST + the post-action history refetch.
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    });

    it("reports a plain requeued-count message when nothing was skipped", async () => {
      mockFetchWithAction(historyResponse, () => ({
        ok: true,
        body: {
          requeued: [{ id: "job-1", file_path: failedJob.file_path, status: "pending" }],
          skipped: [],
        },
      }));
      render(<HistoryPage />);

      fireEvent.click(await screen.findByRole("button", { name: /requeue all failed/i }));
      const confirmPanel = (await screen.findByText(/requeue every failed job\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      fireEvent.click(within(confirmPanel).getByRole("button", { name: /confirm requeue all failed/i }));

      expect(await screen.findByText(/^1 of 1 requeued\.$/i)).toBeInTheDocument();
    });

    it("keeps the confirm step open and shows an error notice when the bulk requeue request fails", async () => {
      mockFetchWithAction(historyResponse, () => ({
        ok: false,
        status: 500,
        body: { detail: "boom" },
      }));
      render(<HistoryPage />);

      fireEvent.click(await screen.findByRole("button", { name: /requeue all failed/i }));
      const confirmPanel = (await screen.findByText(/requeue every failed job\?/i)).closest(
        ".view__confirm",
      ) as HTMLElement;
      fireEvent.click(within(confirmPanel).getByRole("button", { name: /confirm requeue all failed/i }));

      expect(await screen.findByText(/boom/i)).toBeInTheDocument();
      // Still open for a retry, unlike the success path above.
      expect(screen.getByText(/requeue every failed job\?/i)).toBeInTheDocument();
    });
  });
});
