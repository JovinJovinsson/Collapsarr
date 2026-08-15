import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SelfUpdateProgress } from "../components/SelfUpdateProgress";
import { selfUpdatePhaseCopy } from "../utils/selfUpdatePhaseCopy";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const STATUS_URL = "/api/system/self-update/status";

/**
 * Queues a sequence of `GET /api/system/self-update/status` responses (one
 * per call, the last repeats once exhausted) -- mirrors `queuePage.test.tsx`'s
 * `mockFetchQueue` shape for a single-endpoint polling loop.
 */
function mockStatusFetch(
  responses: Array<{ in_progress: boolean; phase: string; previous_version: string | null } | "network-error">
) {
  let call = 0;
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url !== STATUS_URL) {
      throw new Error(`selfUpdateProgress.test.tsx: unhandled fetch call ${url}`);
    }
    const entry = responses[Math.min(call, responses.length - 1)];
    call += 1;
    if (entry === "network-error") {
      return Promise.reject(new TypeError("Failed to fetch"));
    }
    return Promise.resolve(jsonResponse(entry));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * jsdom's `window.location.reload` isn't a plain configurable property `vi.
 * spyOn` can redefine directly ("Cannot redefine property: reload") -- this
 * swaps the whole `window.location` object for one whose `reload` is a spy,
 * mirroring the common jsdom+Vitest workaround for stubbing navigation
 * methods. Restored (`configurable: true`) so later tests get a fresh spy.
 */
function mockLocationReload() {
  const reload = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload },
  });
  return reload;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("selfUpdatePhaseCopy", () => {
  it("maps 'preparing' to a job-count-aware waiting message", () => {
    expect(selfUpdatePhaseCopy("preparing", 3)).toBe("Waiting for 3 jobs to finish…");
    expect(selfUpdatePhaseCopy("preparing", 1)).toBe("Waiting for 1 job to finish…");
    expect(selfUpdatePhaseCopy("preparing", 0)).toBe("Waiting for 0 jobs to finish…");
  });

  it("maps every other known phase to its own distinct copy", () => {
    expect(selfUpdatePhaseCopy("downloading", 0)).toBe("Downloading update…");
    expect(selfUpdatePhaseCopy("verifying", 0)).toBe("Verifying update…");
    expect(selfUpdatePhaseCopy("applying", 0)).toBe("Installing update…");
    expect(selfUpdatePhaseCopy("awaiting_health", 0)).toBe("Restarting…");
    expect(selfUpdatePhaseCopy("idle", 0)).toBe("Finishing up…");
  });

  it("falls back to generic copy for an unrecognized phase", () => {
    expect(selfUpdatePhaseCopy("some_future_phase", 0)).toBe("Updating…");
  });
});

describe("SelfUpdateProgress", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("shows the phase reported by the status endpoint, and re-polls it", async () => {
    const fetchMock = mockStatusFetch([
      { in_progress: true, phase: "downloading", previous_version: "1.2.3" },
      { in_progress: true, phase: "verifying", previous_version: "1.2.3" },
    ]);
    render(<SelfUpdateProgress runningJobCount={0} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Downloading update…")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText("Verifying update…")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows the initial 'preparing' copy with the running Job count before the first poll resolves", async () => {
    mockStatusFetch([{ in_progress: true, phase: "preparing", previous_version: null }]);
    render(<SelfUpdateProgress runningJobCount={2} />);

    expect(screen.getByText("Waiting for 2 jobs to finish…")).toBeInTheDocument();

    // Flush the first poll's already-queued microtask/state update so it
    // lands inside `act` rather than after the test body returns.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  });

  it("keeps showing the last known phase and keeps polling across a network-error gap", async () => {
    const fetchMock = mockStatusFetch([
      { in_progress: true, phase: "applying", previous_version: "1.2.3" },
      "network-error",
      "network-error",
      { in_progress: true, phase: "awaiting_health", previous_version: "1.2.3" },
    ]);
    render(<SelfUpdateProgress runningJobCount={0} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Installing update…")).toBeInTheDocument();

    // First network-error poll: keeps the last known phase visible, and
    // surfaces a "still restarting" footnote rather than a dead end.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText("Installing update…")).toBeInTheDocument();
    expect(screen.getByText(/collapsarr is restarting/i)).toBeInTheDocument();

    // Second network-error poll: still recovers, no terminal/dead state.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText("Installing update…")).toBeInTheDocument();

    // The app comes back up -- a successful read clears the footnote and
    // moves the phase on.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(screen.getByText("Restarting…")).toBeInTheDocument();
    expect(screen.queryByText(/collapsarr is restarting/i)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("reloads the page once the status reports idle and no attempt in progress", async () => {
    const reloadSpy = mockLocationReload();
    mockStatusFetch([
      { in_progress: true, phase: "awaiting_health", previous_version: "1.2.3" },
      { in_progress: false, phase: "idle", previous_version: "1.2.3" },
    ]);
    render(<SelfUpdateProgress runningJobCount={0} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(reloadSpy).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it("shows the rollback terminal state instead of reloading, and stops polling", async () => {
    const reloadSpy = mockLocationReload();
    const fetchMock = mockStatusFetch([
      { in_progress: true, phase: "awaiting_health", previous_version: "1.2.3" },
      { in_progress: false, phase: "rolled_back", previous_version: "1.2.3" },
    ]);
    render(<SelfUpdateProgress runningJobCount={0} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Update failed — rolled back to v1.2.3."
    );
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(screen.queryByText("Restarting…")).not.toBeInTheDocument();

    const callsAtTerminal = fetchMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchMock.mock.calls.length).toBe(callsAtTerminal);
  });

  it("stops polling once the component unmounts", async () => {
    const fetchMock = mockStatusFetch([
      { in_progress: true, phase: "downloading", previous_version: "1.2.3" },
      { in_progress: true, phase: "verifying", previous_version: "1.2.3" },
    ]);
    const { unmount } = render(<SelfUpdateProgress runningJobCount={0} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
