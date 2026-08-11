import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActivityPage } from "../pages/ActivityPage";
import type { JobHistoryEntry } from "../types/activity";

const historyResponse: JobHistoryEntry[] = [
  {
    id: 1,
    job_id: "11111111-1111-1111-1111-111111111111",
    file_path: "/media/movies/Interstellar (2014)/Interstellar.mkv",
    status: "succeeded",
    kind: "downmix",
    started_at: "2026-07-10T10:00:00Z",
    ended_at: "2026-07-10T10:05:00Z",
    exit_code: 0,
    error_text: null,
    target: "5.1",
    language: "en",
    created_at: "2026-07-10T10:00:00Z",
    updated_at: "2026-07-10T10:05:00Z",
  },
  {
    id: 2,
    job_id: "22222222-2222-2222-2222-222222222222",
    file_path: "/media/tv/Show/Season 01/Show.S01E01.mkv",
    status: "failed",
    kind: "set_default_audio",
    started_at: "2026-07-11T08:00:00Z",
    ended_at: "2026-07-11T08:01:00Z",
    exit_code: 1,
    error_text: "ffmpeg exited with an error",
    target: "stereo",
    language: "fr",
    created_at: "2026-07-11T08:00:00Z",
    updated_at: "2026-07-11T08:01:00Z",
  },
];

function mockFetchResolved(body: unknown, ok = true, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok,
      status,
      json: () => Promise.resolve(body),
    }),
  );
}

function mockFetchRejected(error: Error) {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
}

describe("ActivityPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists job history with status, timestamps, exit code, error text, target/language", async () => {
    mockFetchResolved(historyResponse);
    render(<ActivityPage />);

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

  it("shows each row's job kind, including a historical downmix-kind row (COL-155, COL-157)", async () => {
    mockFetchResolved(historyResponse);
    render(<ActivityPage />);

    const succeededRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
    expect(within(succeededRow).getByText("Downmix")).toBeInTheDocument();

    const failedRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
    expect(within(failedRow).getByText("Set Default Audio Track")).toBeInTheDocument();
  });

  it("filters by file path (case-insensitive substring)", async () => {
    mockFetchResolved(historyResponse);
    render(<ActivityPage />);

    await screen.findByText("Interstellar");

    fireEvent.change(screen.getByLabelText(/filter by file path/i), {
      target: { value: "interstellar" },
    });

    expect(screen.getByText("Interstellar")).toBeInTheDocument();
    expect(screen.queryByText("Show.S01E01")).not.toBeInTheDocument();
  });

  it("filters by status", async () => {
    mockFetchResolved(historyResponse);
    render(<ActivityPage />);

    await screen.findByText("Interstellar");

    fireEvent.change(screen.getByLabelText(/filter by status/i), {
      target: { value: "failed" },
    });

    expect(screen.queryByText("Interstellar")).not.toBeInTheDocument();
    expect(screen.getByText("Show.S01E01")).toBeInTheDocument();
  });

  it("shows a dedicated message when filters exclude every row", async () => {
    mockFetchResolved(historyResponse);
    render(<ActivityPage />);

    await screen.findByText("Interstellar");

    fireEvent.change(screen.getByLabelText(/filter by file path/i), {
      target: { value: "nonexistent-file" },
    });

    expect(await screen.findByText(/no job history matches the current filters/i)).toBeInTheDocument();
  });

  it("renders a sensible empty state when no jobs have run", async () => {
    mockFetchResolved([]);
    render(<ActivityPage />);

    expect(await screen.findByText(/no activity yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders an error state when the request fails", async () => {
    mockFetchRejected(new Error("network down"));
    render(<ActivityPage />);

    expect(await screen.findByText(/couldn't load job history: network down/i)).toBeInTheDocument();
  });

  it("renders an error state on a non-ok response", async () => {
    mockFetchResolved({}, false, 500);
    render(<ActivityPage />);

    expect(await screen.findByText(/failed to load job history \(500\)/i)).toBeInTheDocument();
  });

  it("shows a short error inline with no 'Show more' action", async () => {
    mockFetchResolved(historyResponse);
    render(<ActivityPage />);

    const failedRow = (await screen.findByText("Show.S01E01")).closest("tr");
    expect(within(failedRow as HTMLElement).getByText("ffmpeg exited with an error")).toBeInTheDocument();
    expect(within(failedRow as HTMLElement).queryByText(/show more/i)).not.toBeInTheDocument();
  });

  it("truncates a huge error with a 'Show more' modal showing the full text", async () => {
    const hugeError = "ffmpeg stderr: ".repeat(20).trim();
    mockFetchResolved([
      { ...historyResponse[1], id: 3, file_path: "/media/tv/Big/Big.S01E01.mkv", error_text: hugeError },
    ]);
    render(<ActivityPage />);

    const row = (await screen.findByText("Big.S01E01")).closest("tr");
    expect(within(row as HTMLElement).getByRole("button", { name: /show more/i })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: /show more/i }));

    const dialog = await screen.findByRole("dialog", { name: /big/i });
    expect(within(dialog).getByText(hugeError)).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the most recently queued job at the top of the list (COL-108)", async () => {
    mockFetchResolved(historyResponse);
    render(<ActivityPage />);

    await screen.findByText("Interstellar");

    const titles = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.querySelector(".activity-table__title")?.textContent);
    expect(titles).toEqual(["Show.S01E01", "Interstellar"]);
  });

  it("shows a still-pending job (no timestamps yet) at the top of the list", async () => {
    mockFetchResolved([
      ...historyResponse,
      {
        id: 3,
        job_id: "33333333-3333-3333-3333-333333333333",
        file_path: "/media/tv/Newest/Newest.S01E01.mkv",
        status: "pending",
        kind: "downmix",
        started_at: null,
        ended_at: null,
        exit_code: null,
        error_text: null,
        target: null,
        language: null,
        created_at: "2026-07-12T00:00:00Z",
        updated_at: "2026-07-12T00:00:00Z",
      },
    ]);
    render(<ActivityPage />);

    const pendingTitle = await screen.findByText("Newest.S01E01");
    const pendingRow = pendingTitle.closest("tr");
    expect(pendingRow).not.toBeNull();
    const scoped = within(pendingRow as HTMLElement);
    expect(scoped.getByText("Pending")).toBeInTheDocument();
    expect(scoped.getAllByText("—").length).toBeGreaterThan(0);

    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("Newest.S01E01")).toBeInTheDocument();
  });
});
