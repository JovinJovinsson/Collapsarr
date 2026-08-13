import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WantedPage } from "../pages/WantedPage";
import type { WantedFile } from "../types/wanted";

/** `WantedPage` links each row to `/files/:fileId` (COL-34, COL-203), so it needs a router. */
function renderWantedPage() {
  return render(
    <MemoryRouter>
      <WantedPage />
    </MemoryRouter>,
  );
}

const wantedResponse: WantedFile[] = [
  {
    id: 1,
    file_path: "/media/movies/Interstellar (2014)/Interstellar.mkv",
    missing_targets: [
      { language: "en", target: "5.1" },
      { language: "en", target: "2.1" },
    ],
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-02T00:00:00Z",
    library_node_id: null,
    node_type: null,
    tracked: null,
  },
  {
    id: 2,
    file_path: "/media/tv/Show/Season 01/Show.S01E01.mkv",
    missing_targets: [{ language: "fr", target: "stereo" }],
    created_at: "2026-07-03T00:00:00Z",
    updated_at: "2026-07-03T00:00:00Z",
    library_node_id: null,
    node_type: null,
    tracked: null,
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

/**
 * Routes `GET /api/wanted` to `wantedResponse` (the initial list load) and
 * everything else -- the "Queue now" action's `POST /api/jobs/trigger` --
 * through `triggerHandler`, which inspects the request itself. Mirrors
 * `queuePage.test.tsx`'s `mockFetchWithAction` shape.
 */
function mockFetchWithTrigger(
  triggerHandler: (url: string, init?: RequestInit) => { ok: boolean; status?: number; body: unknown },
) {
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (typeof url === "string" && url.includes("/api/wanted")) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(wantedResponse) });
    }
    const { ok, status = 200, body } = triggerHandler(url, init);
    return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("WantedPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists wanted files with title, path, and missing target(s)", async () => {
    mockFetchResolved(wantedResponse);
    renderWantedPage();

    const titleCell = await screen.findByText("Interstellar");
    const row = titleCell.closest("tr");
    expect(row).not.toBeNull();

    const scoped = within(row as HTMLElement);
    expect(
      scoped.getByText("/media/movies/Interstellar (2014)/Interstellar.mkv"),
    ).toBeInTheDocument();
    expect(scoped.getByText("en · 5.1")).toBeInTheDocument();
    expect(scoped.getByText("en · 2.1")).toBeInTheDocument();

    expect(screen.getByText("Show.S01E01")).toBeInTheDocument();
    expect(screen.getByText("fr · Stereo")).toBeInTheDocument();
  });

  it("renders a sensible empty state when nothing is wanted", async () => {
    mockFetchResolved([]);
    renderWantedPage();

    expect(await screen.findByText(/nothing wanted right now/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders an error state when the request fails", async () => {
    mockFetchRejected(new Error("network down"));
    renderWantedPage();

    expect(await screen.findByText(/couldn't load the wanted list: network down/i)).toBeInTheDocument();
  });

  it("renders an error state on a non-ok response", async () => {
    mockFetchResolved({}, false, 500);
    renderWantedPage();

    expect(await screen.findByText(/failed to load wanted list \(500\)/i)).toBeInTheDocument();
  });

  describe('"Queue now" action (COL-195)', () => {
    it("shows a \"Queue now\" action on every row", async () => {
      mockFetchResolved(wantedResponse);
      renderWantedPage();

      const firstRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      expect(within(firstRow).getByRole("button", { name: /queue now/i })).toBeInTheDocument();

      const secondRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
      expect(within(secondRow).getByRole("button", { name: /queue now/i })).toBeInTheDocument();
    });

    it("calls the trigger endpoint with the row's file path and shows a success notice on enqueue", async () => {
      const fetchMock = mockFetchWithTrigger((url, init) => {
        expect(url).toContain("/api/jobs/trigger");
        expect(url).not.toContain("trigger-default-audio");
        expect(init?.method).toBe("POST");
        expect(JSON.parse(init?.body as string)).toEqual({
          file_path: wantedResponse[0].file_path,
        });
        return {
          ok: true,
          body: {
            enqueued: true,
            job: { id: "job-1", file_path: wantedResponse[0].file_path, status: "pending" },
          },
        };
      });

      renderWantedPage();

      const row = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      fireEvent.click(within(row).getByRole("button", { name: /queue now/i }));

      expect(await screen.findByText(/"Interstellar" queued — job job-1 \(Pending\)\./i)).toBeInTheDocument();
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      // The row survives -- queuing doesn't remove it from the Wanted list --
      // and its button is back to normal, not stuck mid-action.
      expect(within(row).getByRole("button", { name: /queue now/i })).not.toBeDisabled();
    });

    it("shows a hint notice, not an error, when the file was skipped (enqueued: false)", async () => {
      mockFetchWithTrigger(() => ({ ok: true, body: { enqueued: false, job: null } }));
      renderWantedPage();

      const row = (await screen.findByText("Show.S01E01")).closest("tr") as HTMLElement;
      fireEvent.click(within(row).getByRole("button", { name: /queue now/i }));

      expect(
        await screen.findByText(/"Show\.S01E01" wasn't queued — the file was skipped/i),
      ).toBeInTheDocument();
    });

    it("shows an error notice when the trigger request fails", async () => {
      mockFetchWithTrigger(() => ({ ok: false, status: 500, body: { detail: "boom" } }));
      renderWantedPage();

      const row = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      fireEvent.click(within(row).getByRole("button", { name: /queue now/i }));

      expect(await screen.findByText(/boom/i)).toBeInTheDocument();
      expect(within(row).getByRole("button", { name: /queue now/i })).not.toBeDisabled();
    });

    it("disables only the clicked row's button while its request is in flight", async () => {
      let resolveTrigger: (value: { ok: boolean; status?: number; body: unknown }) => void = () => {};
      const pending = new Promise<{ ok: boolean; status?: number; body: unknown }>((resolve) => {
        resolveTrigger = resolve;
      });
      const fetchMock = vi.fn().mockImplementation((url: string) => {
        if (typeof url === "string" && url.includes("/api/wanted")) {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(wantedResponse) });
        }
        return pending.then(({ ok, status = 200, body }) => ({ ok, status, json: () => Promise.resolve(body) }));
      });
      vi.stubGlobal("fetch", fetchMock);

      renderWantedPage();

      const firstRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
      const secondRow = screen.getByText("Show.S01E01").closest("tr") as HTMLElement;
      fireEvent.click(within(firstRow).getByRole("button", { name: /queue now/i }));

      expect(await within(firstRow).findByRole("button", { name: /queuing/i })).toBeDisabled();
      expect(within(secondRow).getByRole("button", { name: /queue now/i })).not.toBeDisabled();

      resolveTrigger({ ok: true, body: { enqueued: true, job: { id: "job-2", file_path: "x", status: "pending" } } });
      await waitFor(() => expect(within(firstRow).getByRole("button", { name: /queue now/i })).not.toBeDisabled());
    });
  });
});
