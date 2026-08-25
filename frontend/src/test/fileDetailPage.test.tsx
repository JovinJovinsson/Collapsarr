import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FileDetailPage } from "../pages/FileDetailPage";
import type { JobHistoryEntry } from "../types/activity";
import type { AudioStreamsResponse } from "../types/audioStreams";
import type { GlobalSettings } from "../types/settings";
import type { WantedFile } from "../types/wanted";

const FILE_PATH = "/media/movies/Interstellar (2014)/Interstellar.mkv";

const fileResponse: WantedFile = {
  id: 1,
  file_path: FILE_PATH,
  missing_targets: [{ language: "en", target: "5.1" }],
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-02T00:00:00Z",
  library_node_id: 42,
  node_type: "movie",
  tracked: true,
};

const historyResponse: JobHistoryEntry[] = [
  {
    id: 10,
    job_id: "11111111-1111-1111-1111-111111111111",
    file_path: FILE_PATH,
    status: "succeeded",
    kind: "downmix",
    priority: 0,
    scheduled_at: null,
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

const audioStreamsResponse: AudioStreamsResponse = {
  probeable: true,
  error: null,
  streams: [
    { index: 0, codec: "eac3", channels: 6, channel_layout: "5.1", language: "eng", is_default: true },
    { index: 1, codec: "aac", channels: 2, channel_layout: "stereo", language: "jpn", is_default: false },
  ],
};

const settingsResponse: GlobalSettings = {
  enabled_targets: ["stereo", "5.1"],
  language_allow_list: ["en", "fr"],
  stereo_codec: "aac",
  stereo_bitrate_kbps: 192,
  surround_codec: "eac3",
  surround_bitrate_kbps: 640,
  concurrency_limit: 1,
  ui_auth_enabled: false,
  auth_required: "local_bypass",
  auth_method: "forms",
  backup_interval_days: 7,
  backup_retention_days: 28,
  disk_space_warning_percent: 5,
  disk_space_error_percent: 2,
  update_channel: "stable",
  default_tracked: true,
  log_level: null,
  default_audio_language: null,
  default_audio_channel_tier: null,
  auto_set_default_audio: false,
  recently_processed_window_minutes: 360,
  auto_queue_paused: false,
  auto_processing_paused: false,
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
    file: unknown;
    fileNotFound: boolean;
    history: unknown;
    settings: unknown;
    audioStreams: unknown;
    audioStreamsNotFound: boolean;
    trigger: { ok: boolean; status?: number; body: unknown };
    defaultAudioTrigger: { ok: boolean; status?: number; body: unknown };
    tracked: { ok: boolean; status?: number; body: unknown };
    poster: { ok: boolean; status?: number; body: unknown };
  }> = {},
): Handler {
  return (url, init) => {
    if (url.endsWith("/audio-streams")) {
      if (overrides.audioStreamsNotFound) {
        return { ok: false, status: 404, body: { detail: "No tracked file with this id." } };
      }
      return { ok: true, body: overrides.audioStreams ?? audioStreamsResponse };
    }
    if (url.endsWith("/poster")) {
      return (
        overrides.poster ?? {
          ok: true,
          body: { file_id: 1, status: "placeholder", poster_url: null },
        }
      );
    }
    if (url === "/api/jobs/trigger-default-audio") {
      return (
        overrides.defaultAudioTrigger ?? {
          ok: true,
          body: { enqueued: true, job: { id: "default-audio-job-1", file_path: FILE_PATH, status: "pending" } },
        }
      );
    }
    if (url.startsWith("/api/jobs/trigger")) {
      return overrides.trigger ?? { ok: true, body: { enqueued: true, job: { id: "job-1", file_path: FILE_PATH, status: "pending" } } };
    }
    if (url.startsWith("/api/jobs/history")) {
      return { ok: true, body: overrides.history ?? historyResponse };
    }
    if (url.startsWith("/api/settings")) {
      return { ok: true, body: overrides.settings ?? settingsResponse };
    }
    if (url === "/api/library/tracked") {
      if (overrides.tracked) return overrides.tracked;
      const parsed = JSON.parse(String(init?.body)) as {
        references: { node_type: string; node_id: number }[];
        tracked: boolean;
      };
      return {
        ok: true,
        body: {
          updated: parsed.references.map((r) => ({
            id: r.node_id,
            kind: r.node_type,
            tracked: parsed.tracked,
          })),
        },
      };
    }
    if (url.startsWith("/api/files/")) {
      if (overrides.fileNotFound) {
        return { ok: false, status: 404, body: { detail: "No tracked file with this id." } };
      }
      return { ok: true, body: overrides.file ?? fileResponse };
    }
    throw new Error(`Unexpected fetch: ${String(init?.method ?? "GET")} ${url}`);
  };
}

function renderFileDetailPage(fileId = "1") {
  return render(
    <MemoryRouter initialEntries={[`/files/${fileId}`]}>
      <Routes>
        <Route path="/files/:fileId" element={<FileDetailPage />} />
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

  it("renders a not-found state when the endpoint returns a 404", async () => {
    mockFetchRouter(defaultHandler({ fileNotFound: true }));
    renderFileDetailPage("999");

    expect(await screen.findByText(/no tracked file exists with this id/i)).toBeInTheDocument();
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

  // --- Set Default Audio Track (COL-155, COL-157) -----------------------------

  it('"Set Default Audio Track" calls the single-file default-audio trigger endpoint and reflects the resulting job\'s queued state', async () => {
    const { calls } = mockFetchRouter(
      defaultHandler({
        defaultAudioTrigger: {
          ok: true,
          body: { enqueued: true, job: { id: "default-audio-job-42", file_path: FILE_PATH, status: "pending" } },
        },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /^set default audio track$/i }));

    expect(await screen.findByText(/default-audio-job-42/i)).toBeInTheDocument();
    expect(await screen.findByText("Queued")).toBeInTheDocument();

    const triggerCall = calls.find((call) => call.url === "/api/jobs/trigger-default-audio");
    expect(triggerCall).toBeDefined();
    expect(triggerCall?.init?.method).toBe("POST");
    expect(JSON.parse(String(triggerCall?.init?.body))).toEqual({ file_path: FILE_PATH });
  });

  it('shows a skipped message when "Set Default Audio Track" enqueues no job', async () => {
    mockFetchRouter(
      defaultHandler({ defaultAudioTrigger: { ok: true, body: { enqueued: false, job: null } } }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /^set default audio track$/i }));

    expect(await screen.findByText(/no job enqueued/i)).toBeInTheDocument();
  });

  it.each([
    ["no_preference", /no preferred default audio setting is configured yet/i],
    ["already_correct", /this file's default audio track is already set correctly/i],
    ["unprobeable", /could not be probed/i],
    ["duplicate", /already queued, running, or was processed too recently/i],
  ] as const)(
    'shows a specific message for the "%s" skip reason (COL-207)',
    async (skip_reason, expectedText) => {
      mockFetchRouter(
        defaultHandler({
          defaultAudioTrigger: { ok: true, body: { enqueued: false, job: null, skip_reason } },
        }),
      );
      renderFileDetailPage("1");

      await screen.findByText("Interstellar");
      fireEvent.click(screen.getByRole("button", { name: /^set default audio track$/i }));

      expect(await screen.findByText(/no job enqueued/i)).toBeInTheDocument();
      expect(await screen.findByText(expectedText)).toBeInTheDocument();
    },
  );

  it('shows an error message when the "Set Default Audio Track" request fails', async () => {
    mockFetchRouter(
      defaultHandler({
        defaultAudioTrigger: { ok: false, status: 503, body: { detail: "Job scheduler is not available." } },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /^set default audio track$/i }));

    expect(
      await screen.findByText(/couldn't trigger set default audio track: job scheduler is not available\./i),
    ).toBeInTheDocument();
  });

  it('disables the "Set Default Audio Track" button while the request is in flight', async () => {
    let resolveTrigger!: (value: { ok: boolean; status?: number; json: () => Promise<unknown> }) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url === "/api/jobs/trigger-default-audio") {
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
    const button = screen.getByRole("button", { name: /^set default audio track$/i });
    fireEvent.click(button);

    await waitFor(() => expect(screen.getByRole("button", { name: /setting…/i })).toBeDisabled());

    resolveTrigger({
      ok: true,
      status: 202,
      json: () => Promise.resolve({ enqueued: true, job: { id: "default-audio-job-9", file_path: FILE_PATH, status: "pending" } }),
    });

    expect(await screen.findByText("Queued")).toBeInTheDocument();
  });

  it("shows each status row's job kind, distinguishing a Downmix attempt from a Set Default Audio Track attempt", async () => {
    mockFetchRouter(
      defaultHandler({
        history: [
          ...historyResponse,
          {
            id: 11,
            job_id: "22222222-2222-2222-2222-222222222222",
            file_path: FILE_PATH,
            status: "succeeded",
            kind: "set_default_audio",
            started_at: "2026-07-11T10:00:00Z",
            ended_at: "2026-07-11T10:01:00Z",
            exit_code: 0,
            error_text: null,
            target: "5.1",
            language: "en",
            created_at: "2026-07-11T10:00:00Z",
            updated_at: "2026-07-11T10:01:00Z",
          },
        ],
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    const downmixRow = (await screen.findByText("Stereo")).closest("tr") as HTMLElement;
    expect(within(downmixRow).getByText("Downmix")).toBeInTheDocument();

    const defaultAudioRow = (await screen.findByText("5.1")).closest("tr") as HTMLElement;
    expect(within(defaultAudioRow).getByText("Set Default Audio Track")).toBeInTheDocument();
  });

  it("resolves a row's kind to the latest attempt when a Set Default Audio Track job and a Downmix job land on the same language/target pair", async () => {
    // Both entries key to "en::stereo" -- `buildStatusRows` keeps only the
    // latest (last in the oldest-to-newest history array), so this row
    // should show `set_default_audio`'s kind, not `downmix`'s, even though
    // the downmix entry (id 10, from `historyResponse`) is seeded first.
    mockFetchRouter(
      defaultHandler({
        history: [
          ...historyResponse,
          {
            id: 11,
            job_id: "22222222-2222-2222-2222-222222222222",
            file_path: FILE_PATH,
            status: "succeeded",
            kind: "set_default_audio",
            started_at: "2026-07-11T10:00:00Z",
            ended_at: "2026-07-11T10:01:00Z",
            exit_code: 0,
            error_text: null,
            target: "stereo",
            language: "en",
            created_at: "2026-07-11T10:00:00Z",
            updated_at: "2026-07-11T10:01:00Z",
          },
        ],
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    const stereoRow = (await screen.findByText("Stereo")).closest("tr") as HTMLElement;
    expect(within(stereoRow).getByText("Set Default Audio Track")).toBeInTheDocument();
    expect(within(stereoRow).queryByText("Downmix")).not.toBeInTheDocument();
  });

  // --- Tracked indicator + toggle (COL-101) -----------------------------------

  it("shows the bridged file's current Tracked status", async () => {
    mockFetchRouter(defaultHandler());
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    expect(screen.getByRole("button", { name: /^tracked$/i })).toBeInTheDocument();
  });

  it("toggling Tracked calls the bulk-update endpoint with this file's node reference and flips the indicator", async () => {
    const { calls } = mockFetchRouter(defaultHandler());
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /^tracked$/i }));

    const trackedCall = await vi.waitFor(() => {
      const match = calls.find((call) => call.url === "/api/library/tracked");
      if (!match) throw new Error("not yet called");
      return match;
    });
    expect(trackedCall.init?.method).toBe("POST");
    expect(JSON.parse(String(trackedCall.init?.body))).toEqual({
      references: [{ node_type: "movie", node_id: 42 }],
      tracked: false,
    });

    expect(await screen.findByRole("button", { name: /^not tracked$/i })).toBeInTheDocument();
  });

  it("shows a status-unavailable message when the file has no resolved Library node bridge", async () => {
    mockFetchRouter(
      defaultHandler({
        file: {
          ...fileResponse,
          library_node_id: null,
          node_type: null,
          tracked: null,
        },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    expect(screen.getByText(/tracked status is unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /tracked/i })).not.toBeInTheDocument();
  });

  it("shows an inline error and leaves the indicator unchanged when the Tracked update fails", async () => {
    mockFetchRouter(
      defaultHandler({ tracked: { ok: false, status: 500, body: { detail: "boom" } } }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /^tracked$/i }));

    expect(await screen.findByText(/couldn't update tracked: boom/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^tracked$/i })).not.toBeDisabled();
  });

  // --- Current audio streams (COL-204) ----------------------------------------

  it("shows every current audio stream's language and channel count, with the current Default Audio Track indicated", async () => {
    mockFetchRouter(defaultHandler());
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    const defaultRow = (await screen.findByText("eng")).closest("tr") as HTMLElement;
    expect(within(defaultRow).getByText(/5\.1 \(6ch\)/)).toBeInTheDocument();
    expect(within(defaultRow).getByText("eac3")).toBeInTheDocument();
    expect(within(defaultRow).getByText("Default")).toBeInTheDocument();

    const nonDefaultRow = (await screen.findByText("jpn")).closest("tr") as HTMLElement;
    expect(within(nonDefaultRow).getByText(/stereo \(2ch\)/)).toBeInTheDocument();
    expect(within(nonDefaultRow).queryByText("Default")).not.toBeInTheDocument();
  });

  it("degrades gracefully instead of crashing when the file can't currently be probed (e.g. missing on disk)", async () => {
    mockFetchRouter(
      defaultHandler({
        audioStreams: { probeable: false, error: "no such file: '/media/gone.mkv'", streams: [] },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    expect(
      await screen.findByText(/couldn't be probed right now.*no such file/i),
    ).toBeInTheDocument();
  });

  it("shows an error message rather than crashing when the audio-streams request itself fails", async () => {
    mockFetchRouter(
      defaultHandler({ audioStreamsNotFound: true }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    expect(
      await screen.findByText(/couldn't probe audio streams/i),
    ).toBeInTheDocument();
  });

  // --- Poster slot (COL-205) --------------------------------------------------

  it("shows the placeholder graphic when the poster endpoint returns no poster_url", async () => {
    mockFetchRouter(defaultHandler());
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    expect(screen.getByRole("img", { name: /no poster available/i })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /^poster for/i })).not.toBeInTheDocument();
  });

  it("renders the returned image when the poster endpoint resolves a poster_url", async () => {
    mockFetchRouter(
      defaultHandler({
        poster: {
          ok: true,
          body: { file_id: 1, status: "available", poster_url: "https://example.com/poster.jpg" },
        },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    const image = (await screen.findByRole("img", {
      name: /^poster for interstellar$/i,
    })) as HTMLImageElement;
    expect(image.src).toBe("https://example.com/poster.jpg");
  });

  it("falls back to the placeholder graphic, never an error, when the poster request fails", async () => {
    mockFetchRouter(
      defaultHandler({ poster: { ok: false, status: 500, body: { detail: "boom" } } }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");

    expect(await screen.findByRole("img", { name: /no poster available/i })).toBeInTheDocument();
    expect(screen.queryByText(/boom/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/couldn't load poster/i)).not.toBeInTheDocument();
  });

  it("falls back to the placeholder graphic if the resolved poster image itself fails to load", async () => {
    // Guards the Phase 2 (COL-212) handoff: once a real poster_url is
    // returned, a broken/expired/hotlink-blocked image must still degrade to
    // the placeholder graphic rather than a broken-image glyph.
    mockFetchRouter(
      defaultHandler({
        poster: {
          ok: true,
          body: { file_id: 1, status: "available", poster_url: "https://example.com/poster.jpg" },
        },
      }),
    );
    renderFileDetailPage("1");

    await screen.findByText("Interstellar");
    const image = await screen.findByRole("img", { name: /^poster for interstellar$/i });

    fireEvent.error(image);

    expect(await screen.findByRole("img", { name: /no poster available/i })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /^poster for/i })).not.toBeInTheDocument();
  });
});
