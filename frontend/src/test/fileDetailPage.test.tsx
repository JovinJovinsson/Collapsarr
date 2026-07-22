import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FileDetailPage } from "../pages/FileDetailPage";
import type { JobHistoryEntry } from "../types/activity";
import type { GlobalSettings } from "../types/settings";
import type { WantedFile } from "../types/wanted";

const FILE_PATH = "/media/movies/Interstellar (2014)/Interstellar.mkv";

const wantedResponse: WantedFile[] = [
  {
    id: 1,
    file_path: FILE_PATH,
    missing_targets: [{ language: "en", target: "5.1" }],
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-02T00:00:00Z",
  },
];

const historyResponse: JobHistoryEntry[] = [
  {
    id: 10,
    job_id: "11111111-1111-1111-1111-111111111111",
    file_path: FILE_PATH,
    status: "succeeded",
    started_at: "2026-07-10T10:00:00Z",
    ended_at: "2026-07-10T10:05:00Z",
    exit_code: 0,
    error_text: null,
    target: "stereo",
    language: "en",
    created_at: "2026-07-10T10:00:00Z",
    updated_at: "2026-07-10T10:05:00Z",
  },
];

const settingsResponse: GlobalSettings = {
  enabled_targets: ["stereo", "5.1"],
  language_allow_list: ["en", "fr"],
  stereo_codec: "aac",
  stereo_bitrate_kbps: 192,
  surround_codec: "eac3",
  surround_bitrate_kbps: 640,
  concurrency_limit: 1,
  ui_auth_enabled: false,
  api_key: "test-key",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

interface FetchCall {
  url: string;
  init?: RequestInit;
}

type Handler = (url: string, init?: RequestInit) => { ok: boolean; status?: number; body: unknown };

/** Routes the shared `apiFetch` calls `FileDetailPage` makes to per-URL canned responses. */
function mockFetchRouter(handler: Handler): { calls: FetchCall[] } {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const { ok, status = 200, body } = handler(url, init);
      return Promise.resolve({
        ok,
        status,
        json: () => Promise.resolve(body),
      });
    }),
  );
  return { calls };
}

function defaultHandler(
  overrides: Partial<{
    wanted: unknown;
    history: unknown;
    settings: unknown;
    trigger: { ok: boolean; status?: number; body: unknown };
  }> = {},
): Handler {
  return (url, init) => {
    if (url.startsWith("/api/jobs/trigger")) {
      return overrides.trigger ?? { ok: true, body: { enqueued: true, job: { id: "job-1", file_path: FILE_PATH, status: "pending" } } };
    }
    if (url.startsWith("/api/jobs/history")) {
      return { ok: true, body: overrides.history ?? historyResponse };
    }
    if (url.startsWith("/api/settings")) {
      return { ok: true, body: overrides.settings ?? settingsResponse };
    }
    if (url.startsWith("/api/wanted")) {
      return { ok: true, body: overrides.wanted ?? wantedResponse };
    }
    throw new Error(`Unexpected fetch: ${String(init?.method ?? "GET")} ${url}`);
  };
}

function renderFileDetailPage(fileId = "1") {
  return render(
    <MemoryRouter initialEntries={[`/wanted/${fileId}`]}>
      <Routes>
        <Route path="/wanted/:fileId" element={<FileDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("FileDetailPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the file's current per-target/per-language status", async () => {
    mockFetchRouter(defaultHandler());
    renderFileDetailPage("1");

    expect(await screen.findByText("Interstellar")).toBeInTheDocument();
    expect(screen.getByText(FILE_PATH)).toBeInTheDocument();

    const missingRow = (await screen.findByText("5.1")).closest("tr") as HTMLElement;
    expect(within(missingRow).getByText("Missing")).toBeInTheDocument();
    expect(within(missingRow).getByText("en")).toBeInTheDocument();

    const succeededRow = (await screen.findByText("Stereo")).closest("tr") as HTMLElement;
    expect(within(succeededRow).getByText("Succeeded")).toBeInTheDocument();

    expect(screen.getByText(/global language allow-list/i)).toBeInTheDocument();
    expect(screen.getByText("en, fr")).toBeInTheDocument();
  });

  it("renders a not-found state when no wanted file matches the id", async () => {
    mockFetchRouter(defaultHandler({ wanted: [] }));
    renderFileDetailPage("999");

    expect(
      await screen.findByText(/no tracked file with this id is currently in the wanted list/i),
    ).toBeInTheDocument();
  });

  it("renders an error state when the file fails to load", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    renderFileDetailPage("1");

    expect(await screen.findByText(/couldn't load this file: network down/i)).toBeInTheDocument();
  });

  it('"Trigger downmix" calls the manual-trigger endpoint, including for an allow-list-excluded language, and reflects the resulting job\'s queued state', async () => {
    const { calls } = mockFetchRouter(
      defaultHandler({
        trigger: { ok: true, body: { enqueued: true, job: { id: "job-42", file_path: FILE_PATH, status: "pending" } } },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    const languageInput = screen.getByLabelText(/bypass language allow-list/i);
    fireEvent.change(languageInput, { target: { value: "de" } });

    fireEvent.click(screen.getByRole("button", { name: /trigger downmix/i }));

    expect(await screen.findByText(/job/i)).toBeInTheDocument();
    expect(await screen.findByText("Queued")).toBeInTheDocument();

    const triggerCall = calls.find((call) => call.url === "/api/jobs/trigger");
    expect(triggerCall).toBeDefined();
    expect(triggerCall?.init?.method).toBe("POST");
    const body = JSON.parse(String(triggerCall?.init?.body));
    expect(body).toEqual({ file_path: FILE_PATH, extra_languages: ["de"] });
  });

  it('reflects a "running" job status after triggering', async () => {
    mockFetchRouter(
      defaultHandler({
        trigger: { ok: true, body: { enqueued: true, job: { id: "job-7", file_path: FILE_PATH, status: "running" } } },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /trigger downmix/i }));

    expect(await screen.findByText("Running")).toBeInTheDocument();
  });

  it("shows a skipped message when no job was enqueued", async () => {
    mockFetchRouter(defaultHandler({ trigger: { ok: true, body: { enqueued: false, job: null } } }));
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /trigger downmix/i }));

    expect(await screen.findByText(/no job enqueued/i)).toBeInTheDocument();
  });

  it("shows an error message when the trigger request fails", async () => {
    mockFetchRouter(defaultHandler({ trigger: { ok: false, status: 503, body: { detail: "Job scheduler is not available." } } }));
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /trigger downmix/i }));

    expect(await screen.findByText(/couldn't trigger downmix: job scheduler is not available\./i)).toBeInTheDocument();
  });

  it("disables the trigger button while the request is in flight", async () => {
    let resolveTrigger!: (value: { ok: boolean; status?: number; json: () => Promise<unknown> }) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url === "/api/jobs/trigger") {
          return new Promise((resolve) => {
            resolveTrigger = resolve;
          });
        }
        const handler = defaultHandler();
        const { ok, status = 200, body } = handler(url);
        return Promise.resolve({ ok, status, json: () => Promise.resolve(body) });
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    const button = screen.getByRole("button", { name: /trigger downmix/i });
    fireEvent.click(button);

    await waitFor(() => expect(screen.getByRole("button", { name: /triggering/i })).toBeDisabled());

    resolveTrigger({ ok: true, status: 202, json: () => Promise.resolve({ enqueued: true, job: { id: "job-9", file_path: FILE_PATH, status: "pending" } }) });

    expect(await screen.findByText("Queued")).toBeInTheDocument();
  });
});
