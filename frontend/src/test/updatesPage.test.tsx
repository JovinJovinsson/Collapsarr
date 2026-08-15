import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UpdatesPage } from "../pages/UpdatesPage";
import type { JobHistoryEntry } from "../types/activity";
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
  dismissed_at: null,
  install_method: "pipx",
};

const updateAvailable: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.3.0",
  latest_version_label: "Collapsarr v1.3.0",
  changelog: "- added things\n- fixed things",
  checked_at: "2026-08-02T10:00:00Z",
  update_available: true,
  dismissed_at: null,
  install_method: "pipx",
};

const updateAvailableDocker: UpdateCheckState = {
  ...updateAvailable,
  install_method: "docker",
};

const updateAvailableNative: UpdateCheckState = {
  ...updateAvailable,
  install_method: "native",
};

const updateDismissed: UpdateCheckState = {
  ...updateAvailable,
  dismissed_at: "2026-08-02T11:00:00Z",
};

const neverChecked: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: null,
  latest_version_label: null,
  changelog: null,
  checked_at: null,
  update_available: true,
  dismissed_at: null,
  install_method: "pipx",
};

/**
 * Default `GET /api/settings` body (COL-228): `update_channel: "beta"` --
 * `canOfferSelfUpdate` (`UpdatesPage`) requires a **stable**-channel update
 * (Self-Update ships stable-only), so this default keeps every test that
 * doesn't care about Self-Update on the pre-COL-228 fallback path (manual
 * `UpdateInstructions`), exactly as before COL-228 added the "Update Now"
 * action. Self-Update-specific tests below override this to `"stable"`.
 */
const DEFAULT_SETTINGS_RESPONSE = { update_channel: "beta" };

function runningJob(id: string): JobHistoryEntry {
  return {
    id: 1,
    job_id: id,
    file_path: "/media/movies/Interstellar (2014)/Interstellar.mkv",
    status: "running",
    kind: "downmix",
    priority: 1,
    started_at: "2026-08-15T10:00:00Z",
    ended_at: null,
    exit_code: null,
    error_text: null,
    target: "5.1",
    language: "en",
    created_at: "2026-08-15T09:55:00Z",
    updated_at: "2026-08-15T10:00:00Z",
  };
}

type RouteResult = { ok?: boolean; status?: number; body: unknown };

/**
 * Default `GET /api/system/self-update/status` body -- an attempt already
 * under way (COL-231's `SelfUpdateProgress` only ever mounts once
 * `handleConfirmSelfUpdate` has already called the apply endpoint), so tests
 * that don't care about its polling behavior still see a stable
 * `"preparing"` phase rather than immediately reading as complete.
 */
const DEFAULT_SELF_UPDATE_STATUS = { in_progress: true, phase: "preparing", previous_version: null };

/**
 * Routes every `fetch` call this page makes by URL/method (COL-228) --
 * `GET /api/system/updates` to `updateBody`, `POST .../recheck`/`.../dismiss`/
 * `.../undismiss` to their own override (default: `updateBody` again, mirroring
 * the endpoint's real "returns the refreshed state" contract), `GET
 * /api/settings` to `settings` (default {@link DEFAULT_SETTINGS_RESPONSE}),
 * `GET /api/jobs/queue` to `queue` (default `[]`),
 * `POST /api/system/self-update/apply` to `apply` (default `{ok: true}`,
 * `"network-error"` rejects the call the way a real re-exec/exit drops the
 * connection, COL-231), and `GET /api/system/self-update/status` to
 * `selfUpdateStatus` (default {@link DEFAULT_SELF_UPDATE_STATUS}, COL-231 --
 * `SelfUpdateProgress`'s polling target).
 *
 * URL-routed rather than call-order-indexed (unlike this file's pre-COL-228
 * `mockResolvedValueOnce` chains) because `UpdatesPage` now fires two
 * concurrent fetches on mount (`GET /api/system/updates` *and* `GET
 * /api/settings`, COL-228) -- a positional sequence can no longer assume
 * "first call is the initial load, second call is the button click".
 * Mirrors `queuePage.test.tsx`'s `mockFetch`/`mockFetchWithAction` pattern.
 */
function mockUpdatesFetch(
  updateBody: unknown,
  overrides: {
    settings?: unknown;
    recheck?: RouteResult;
    dismiss?: RouteResult;
    undismiss?: RouteResult;
    queue?: unknown;
    apply?: RouteResult | "network-error" | ((init?: RequestInit) => RouteResult);
    selfUpdateStatus?: unknown;
  } = {}
) {
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";

    if (url === "/api/system/updates" && method === "GET") {
      return Promise.resolve(jsonResponse(updateBody));
    }
    if (url === "/api/system/updates/recheck") {
      const { status = 200, body } = overrides.recheck ?? { body: updateBody };
      return Promise.resolve(jsonResponse(body, status));
    }
    if (url === "/api/system/updates/dismiss") {
      const { status = 200, body } = overrides.dismiss ?? { body: updateBody };
      return Promise.resolve(jsonResponse(body, status));
    }
    if (url === "/api/system/updates/undismiss") {
      const { status = 200, body } = overrides.undismiss ?? { body: updateBody };
      return Promise.resolve(jsonResponse(body, status));
    }
    if (url === "/api/settings" && method === "GET") {
      return Promise.resolve(jsonResponse(overrides.settings ?? DEFAULT_SETTINGS_RESPONSE));
    }
    if (url === "/api/jobs/queue") {
      return Promise.resolve(jsonResponse(overrides.queue ?? []));
    }
    if (url === "/api/system/self-update/apply") {
      const handler = overrides.apply ?? { body: { ok: true } };
      if (handler === "network-error") {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      const { status = 200, body } = typeof handler === "function" ? handler(init) : handler;
      return Promise.resolve(jsonResponse(body, status));
    }
    if (url === "/api/system/self-update/status" && method === "GET") {
      return Promise.resolve(jsonResponse(overrides.selfUpdateStatus ?? DEFAULT_SELF_UPDATE_STATUS));
    }
    throw new Error(`updatesPage.test.tsx: unhandled fetch call ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UpdatesPage", () => {
  it("shows a loading state, then the up-to-date state", async () => {
    mockUpdatesFetch(upToDate);
    render(<UpdatesPage />);

    expect(screen.getByText(/loading update status/i)).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());
    expect(screen.getByText("v1.2.3")).toBeInTheDocument();
    expect(screen.getByText(/you're up to date/i)).toBeInTheDocument();
  });

  it("shows the update-available state, including the changelog", async () => {
    mockUpdatesFetch(updateAvailable);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    expect(screen.getByText(/an update is available/i)).toHaveTextContent("v1.3.0");
    expect(screen.getAllByText("v1.3.0").length).toBeGreaterThan(0);
    expect(screen.getByText("Collapsarr v1.3.0")).toBeInTheDocument();
    expect(screen.getByText(/added things/)).toBeInTheDocument();
  });

  // COL-90: changelog Markdown rendering.

  it("renders changelog Markdown (headings, bullet lists, links) as formatted content", async () => {
    const markdownChangelog: UpdateCheckState = {
      ...updateAvailable,
      changelog: "## Highlights\n\n- fixed a bug\n\nSee the [full notes](https://example.com/notes).",
    };
    mockUpdatesFetch(markdownChangelog);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());

    // A `##` heading becomes a real heading element, not literal "##" text.
    const heading = screen.getByRole("heading", { name: "Highlights" });
    expect(heading.tagName).toBe("H2");
    // A `- ` bullet becomes a real list item.
    const listItem = screen.getByText("fixed a bug");
    expect(listItem.tagName).toBe("LI");
    // A `[text](url)` link becomes a real anchor with the target href.
    const link = screen.getByRole("link", { name: "full notes" });
    expect(link).toHaveAttribute("href", "https://example.com/notes");
  });

  it("never renders raw HTML embedded in the changelog body as HTML", async () => {
    const htmlInjectionChangelog: UpdateCheckState = {
      ...updateAvailable,
      changelog: '<img src="x" onerror="window.__pwned = true" /><script>window.__pwned = true</script>',
    };
    mockUpdatesFetch(htmlInjectionChangelog);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());

    // Neither tag was mounted as a real DOM element -- `rehype-raw` is
    // deliberately not enabled, so react-markdown treats the raw HTML as
    // inert text rather than rendering it (which would otherwise let an
    // externally-sourced changelog inject an `onerror` handler or a
    // `<script>` tag).
    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  // COL-90/COL-219/COL-91/COL-228: install-method instructions, now rendered
  // by the extracted `UpdateInstructions` subcomponent. Every case here keeps
  // the default (non-stable) `update_channel`, so `canOfferSelfUpdate` is
  // `false` and these fall back to the manual instructions exactly as before
  // COL-228 -- self-update-eligible states have their own tests below.

  it("shows docker pull + recreate-container instructions when install_method is docker", async () => {
    mockUpdatesFetch(updateAvailableDocker, { settings: { update_channel: "stable" } });
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/how to update/i)).toBeInTheDocument());
    expect(screen.getByText(/docker pull odxnsson\/collapsarr:1\.3\.0/)).toBeInTheDocument();
    expect(screen.getByText(/docker compose up -d/)).toBeInTheDocument();
    expect(screen.queryByText(/pipx upgrade collapsarr/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pip install --upgrade collapsarr/)).not.toBeInTheDocument();
    expect(screen.queryByText(/release page/i)).not.toBeInTheDocument();
    // Docker never offers Self-Update, even on the stable channel -- an
    // image cannot self-replace.
    expect(screen.queryByRole("button", { name: "Update Now" })).not.toBeInTheDocument();
  });

  it("shows pipx and pip instructions when install_method is pipx", async () => {
    mockUpdatesFetch(updateAvailable);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/how to update/i)).toBeInTheDocument());
    expect(screen.getByText(/pipx upgrade collapsarr/)).toBeInTheDocument();
    expect(screen.getByText(/pip install --upgrade collapsarr/)).toBeInTheDocument();
    expect(screen.queryByText(/docker pull/)).not.toBeInTheDocument();
    expect(screen.queryByText(/release page/i)).not.toBeInTheDocument();
  });

  it("shows download/replace/restart instructions and a data-safety note when install_method is native", async () => {
    mockUpdatesFetch(updateAvailableNative);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/how to update/i)).toBeInTheDocument());
    expect(screen.getByRole("link", { name: /release page/i })).toHaveAttribute(
      "href",
      "https://github.com/JovinJovinsson/Collapsarr/releases"
    );
    expect(screen.getByText(/replace the install folder/i)).toBeInTheDocument();
    expect(screen.getByText(/restart collapsarr/i)).toBeInTheDocument();
    // Data-safety note: DB/config live outside the install folder being replaced.
    expect(screen.getByText(/database and settings are safe/i)).toBeInTheDocument();
    expect(screen.getByText(/os user-data directory/i)).toBeInTheDocument();
    expect(screen.queryByText(/docker pull/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pipx upgrade collapsarr/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pip install --upgrade collapsarr/)).not.toBeInTheDocument();
  });

  it("does not show install instructions when there is no update available", async () => {
    mockUpdatesFetch(upToDate);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
    expect(screen.queryByText(/how to update/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update Now" })).not.toBeInTheDocument();
  });

  it("handles a never-checked state without a latest version", async () => {
    mockUpdatesFetch(neverChecked);
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
    const fetchMock = mockUpdatesFetch(upToDate, { recheck: { body: updateAvailable } });
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Check now" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/system/updates/recheck",
        expect.objectContaining({ method: "POST" })
      )
    );
    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    // Exactly the mount-time loads (update status + settings) plus the
    // recheck round-trip -- no extra GET afterwards, since the recheck
    // response already carries the full, fresh state.
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("shows an error banner when a recheck fails, leaving the stale state in place", async () => {
    mockUpdatesFetch(upToDate, {
      recheck: { ok: false, status: 503, body: { detail: "scheduler unavailable" } },
    });
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Check now" }));

    await waitFor(() => expect(screen.getByText(/scheduler unavailable/i)).toBeInTheDocument());
    expect(screen.getByText(/you're up to date/i)).toBeInTheDocument();
  });

  // COL-89: dismiss/undismiss.

  it("offers a Dismiss button when an update is available and not yet dismissed", async () => {
    mockUpdatesFetch(updateAvailable);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undismiss" })).not.toBeInTheDocument();
    expect(screen.queryByText(/dismissed/i)).not.toBeInTheDocument();
  });

  it("does not offer Dismiss when there is no update available", async () => {
    mockUpdatesFetch(upToDate);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });

  it("dismissing calls the dismiss endpoint and switches to an Undismiss button", async () => {
    const fetchMock = mockUpdatesFetch(updateAvailable, { dismiss: { body: updateDismissed } });
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/system/updates/dismiss",
        expect.objectContaining({ method: "POST" })
      )
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Undismiss" })).toBeInTheDocument()
    );
    expect(screen.getByText(/dismissed/i)).toBeInTheDocument();
  });

  it("undismissing calls the undismiss endpoint and switches back to a Dismiss button", async () => {
    const fetchMock = mockUpdatesFetch(updateDismissed, { undismiss: { body: updateAvailable } });
    render(<UpdatesPage />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Undismiss" })).toBeInTheDocument()
    );
    fireEvent.click(screen.getByRole("button", { name: "Undismiss" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/system/updates/undismiss",
        expect.objectContaining({ method: "POST" })
      )
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument());
    expect(screen.queryByText(/dismissed/i)).not.toBeInTheDocument();
  });

  it("shows an error when a dismiss action fails, leaving the button in place", async () => {
    mockUpdatesFetch(updateAvailable, {
      dismiss: { ok: false, status: 500, body: { detail: "boom" } },
    });
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(screen.getByText(/boom/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  // COL-228: "Update Now" trigger + confirmation modal.

  describe("Self-Update 'Update Now'", () => {
    it("shows 'Update Now' for a pipx install with a stable update available", async () => {
      mockUpdatesFetch(updateAvailable, { settings: { update_channel: "stable" } });
      render(<UpdatesPage />);

      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Update Now" })).toBeInTheDocument()
      );
      // Self-Update supersedes the manual walkthrough when it's offered.
      expect(screen.queryByText(/how to update/i)).not.toBeInTheDocument();
    });

    it("shows 'Update Now' for a native install with a stable update available", async () => {
      mockUpdatesFetch(updateAvailableNative, { settings: { update_channel: "stable" } });
      render(<UpdatesPage />);

      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Update Now" })).toBeInTheDocument()
      );
      expect(screen.queryByText(/how to update/i)).not.toBeInTheDocument();
    });

    it("does not show 'Update Now' for a docker install, even on the stable channel", async () => {
      mockUpdatesFetch(updateAvailableDocker, { settings: { update_channel: "stable" } });
      render(<UpdatesPage />);

      await waitFor(() => expect(screen.getByText(/how to update/i)).toBeInTheDocument());
      expect(screen.queryByRole("button", { name: "Update Now" })).not.toBeInTheDocument();
    });

    it("does not show 'Update Now' when the configured channel is beta, even if an update is available", async () => {
      mockUpdatesFetch(updateAvailable, { settings: { update_channel: "beta" } });
      render(<UpdatesPage />);

      await waitFor(() => expect(screen.getByText(/how to update/i)).toBeInTheDocument());
      expect(screen.queryByRole("button", { name: "Update Now" })).not.toBeInTheDocument();
    });

    it("does not show 'Update Now' when there is no update available, even on the stable channel", async () => {
      mockUpdatesFetch(upToDate, { settings: { update_channel: "stable" } });
      render(<UpdatesPage />);

      await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
      expect(screen.queryByRole("button", { name: "Update Now" })).not.toBeInTheDocument();
    });

    it("clicking 'Update Now' with no Jobs running shows a plain confirmation", async () => {
      mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));

      await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
      expect(
        screen.getByText(/downloads, verifies, and installs the latest stable release/i)
      ).toBeInTheDocument();
      expect(screen.queryByText(/currently running/i)).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Cancel & Restart Now" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Wait & Restart" })).not.toBeInTheDocument();
    });

    it("clicking 'Update Now' with Jobs running shows the running-job count and the two flow choices", async () => {
      mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [runningJob("11111111-1111-1111-1111-111111111111")],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));

      await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
      expect(screen.getByText(/1 job is currently running/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Cancel & Restart Now" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Wait & Restart" })).toBeInTheDocument();
    });

    it("pluralizes the running-job count for more than one running Job", async () => {
      mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [
          runningJob("11111111-1111-1111-1111-111111111111"),
          runningJob("22222222-2222-2222-2222-222222222222"),
        ],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));

      await waitFor(() => expect(screen.getByText(/2 jobs are currently running/i)).toBeInTheDocument());
    });

    // The apply endpoint (`collapsarr/self_update/routes.py`) 409s any
    // non-null `flow` on a `native` install (COL-233's in-flight-Job
    // handling only wraps the `pipx` path so far) -- offering the two flow
    // choices for a native install with Jobs running would just be two
    // guaranteed-`409` buttons, so the modal blocks instead.

    it("blocks with an explanatory message instead of flow choices for a native install with Jobs running", async () => {
      mockUpdatesFetch(updateAvailableNative, {
        settings: { update_channel: "stable" },
        queue: [runningJob("11111111-1111-1111-1111-111111111111")],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));

      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText(/not supported yet for native installs/i)).toBeInTheDocument();
      expect(within(dialog).getByText(/1 job is currently running/i)).toBeInTheDocument();
      expect(
        within(dialog).queryByRole("button", { name: "Cancel & Restart Now" })
      ).not.toBeInTheDocument();
      expect(within(dialog).queryByRole("button", { name: "Wait & Restart" })).not.toBeInTheDocument();
      expect(within(dialog).queryByRole("button", { name: "Update Now" })).not.toBeInTheDocument();
      expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    });

    it("closing the native-blocked modal calls nothing", async () => {
      const fetchMock = mockUpdatesFetch(updateAvailableNative, {
        settings: { update_channel: "stable" },
        queue: [runningJob("11111111-1111-1111-1111-111111111111")],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));
      const dialog = await screen.findByRole("dialog");
      const callsBeforeClose = fetchMock.mock.calls.length;

      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(fetchMock.mock.calls.length).toBe(callsBeforeClose);
      expect(fetchMock).not.toHaveBeenCalledWith(
        "/api/system/self-update/apply",
        expect.anything()
      );
    });

    it("still offers the two flow choices for a pipx install with Jobs running", async () => {
      mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [runningJob("11111111-1111-1111-1111-111111111111")],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));

      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByRole("button", { name: "Cancel & Restart Now" })).toBeInTheDocument();
      expect(within(dialog).getByRole("button", { name: "Wait & Restart" })).toBeInTheDocument();
      expect(within(dialog).queryByText(/not supported yet/i)).not.toBeInTheDocument();
    });

    it("confirming the plain confirm calls the apply endpoint with no flow, then shows the updating screen (COL-231)", async () => {
      const fetchMock = mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Update Now" }));

      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/system/self-update/apply",
          expect.objectContaining({ method: "POST", body: JSON.stringify({}) })
        )
      );
      // A successful apply transitions into the dedicated "Updating — please
      // wait" screen (COL-231) instead of a page-level notice -- the modal
      // closes as part of that transition.
      await waitFor(() =>
        expect(screen.getByText(/updating — please wait/i)).toBeInTheDocument()
      );
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.queryByText(/update triggered/i)).not.toBeInTheDocument();
    });

    it("a network-level failure right after confirming (the connection dropping mid-re-exec) still shows the updating screen, not an error", async () => {
      const fetchMock = mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [],
        apply: "network-error",
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Update Now" }));

      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/system/self-update/apply",
          expect.objectContaining({ method: "POST", body: JSON.stringify({}) })
        )
      );
      await waitFor(() =>
        expect(screen.getByText(/updating — please wait/i)).toBeInTheDocument()
      );
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    it("confirming 'Cancel & Restart Now' calls the apply endpoint with that flow", async () => {
      const fetchMock = mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [runningJob("11111111-1111-1111-1111-111111111111")],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));
      await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: "Cancel & Restart Now" }));

      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/system/self-update/apply",
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({ flow: "cancel_and_restart" }),
          })
        )
      );
    });

    it("confirming 'Wait & Restart' calls the apply endpoint with that flow", async () => {
      const fetchMock = mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [runningJob("11111111-1111-1111-1111-111111111111")],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));
      await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
      fireEvent.click(screen.getByRole("button", { name: "Wait & Restart" }));

      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/system/self-update/apply",
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({ flow: "wait_and_restart" }),
          })
        )
      );
    });

    it("declining (Cancel) the modal calls nothing further, leaving the app untouched", async () => {
      const fetchMock = mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [runningJob("11111111-1111-1111-1111-111111111111")],
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));
      await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
      const callsBeforeCancel = fetchMock.mock.calls.length;

      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      // No apply call, and no further fetch at all, went out.
      expect(fetchMock.mock.calls.length).toBe(callsBeforeCancel);
      expect(fetchMock).not.toHaveBeenCalledWith(
        "/api/system/self-update/apply",
        expect.anything()
      );
    });

    it("shows an apply-endpoint error inside the still-open modal", async () => {
      mockUpdatesFetch(updateAvailable, {
        settings: { update_channel: "stable" },
        queue: [],
        apply: { ok: false, status: 409, body: { detail: "An update attempt is already in progress." } },
      });
      render(<UpdatesPage />);

      fireEvent.click(await screen.findByRole("button", { name: "Update Now" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "Update Now" }));

      await waitFor(() =>
        expect(screen.getByText(/already in progress/i)).toBeInTheDocument()
      );
      // The modal stays open so the operator can see the error and retry/cancel.
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
  });
});
