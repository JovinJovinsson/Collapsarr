import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InstancesProvider } from "../components/InstancesProvider";
import { LibraryPage } from "../pages/LibraryPage";
import type { LibraryTreeResponse, MovieLibraryTreeResponse } from "../types/library";
import type { ArrInstance } from "../types/instances";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

function renderLibraryPage(instanceId: number) {
  return render(
    <MemoryRouter initialEntries={[`/libraries/${instanceId}`]}>
      <InstancesProvider>
        <Routes>
          <Route path="/libraries/:instanceId" element={<LibraryPage />} />
        </Routes>
      </InstancesProvider>
    </MemoryRouter>,
  );
}

const sonarrInstance: ArrInstance = {
  id: 1,
  name: "Sonarr Prod",
  type: "sonarr",
  base_url: "http://localhost:8989",
  api_key: "sonarr-key",
  status: "ok",
  status_error: null,
  status_checked_at: null,
  version: null,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

const radarrInstance: ArrInstance = {
  ...sonarrInstance,
  id: 2,
  name: "Radarr Prod",
  type: "radarr",
  base_url: "http://localhost:7878",
};

const seriesTree: LibraryTreeResponse = {
  instance_id: 1,
  series: [
    {
      id: 10,
      kind: "series",
      sonarr_series_id: 100,
      title: "Breaking Bad",
      tracked: true,
      seasons: [
        {
          id: 20,
          kind: "season",
          season_number: 1,
          tracked: true,
          episodes: [
            {
              id: 30,
              kind: "episode",
              sonarr_episode_id: 300,
              season_number: 1,
              episode_number: 1,
              title: "Pilot",
              has_file: true,
              tracked: true,
              current_default_track: null,
              file_id: null,
            },
            {
              id: 31,
              kind: "episode",
              sonarr_episode_id: 301,
              season_number: 1,
              episode_number: 2,
              title: "Cat's in the Bag...",
              has_file: false,
              tracked: true,
              current_default_track: null,
              file_id: null,
            },
          ],
        },
      ],
    },
  ],
};

/**
 * A two-series tree (COL-103): `seriesTree` above only has one Series/one
 * Season/two Episodes, which can't exercise "check boxes across different
 * series/seasons/levels" -- this adds a second Series branch (with its own
 * Season/Episode) so a test can select rows spanning both series.
 */
const twoSeriesTree: LibraryTreeResponse = {
  instance_id: 1,
  series: [
    ...seriesTree.series,
    {
      id: 11,
      kind: "series",
      sonarr_series_id: 101,
      title: "Better Call Saul",
      tracked: true,
      seasons: [
        {
          id: 21,
          kind: "season",
          season_number: 1,
          tracked: true,
          episodes: [
            {
              id: 32,
              kind: "episode",
              sonarr_episode_id: 302,
              season_number: 1,
              episode_number: 1,
              title: "Uno",
              has_file: true,
              tracked: true,
              current_default_track: null,
              file_id: null,
            },
          ],
        },
      ],
    },
  ],
};

/**
 * A two-series tree with differing top-level resolved Tracked values
 * (COL-104): `twoSeriesTree` above has both series (and everything under
 * them) resolving Tracked, which can't exercise the Tracked filter
 * narrowing anything out. This flips "Better Call Saul" (series, season,
 * and episode alike) to resolve Not Tracked, so search + Tracked-filter
 * tests have a clean per-series split to narrow against -- "Breaking Bad"
 * stays fully Tracked throughout its own subtree, so it never surfaces
 * under a Not-Tracked filter even as ancestor context for some deeper
 * override (there isn't one here).
 */
const mixedTrackedTree: LibraryTreeResponse = {
  instance_id: 1,
  series: [
    seriesTree.series[0],
    {
      id: 11,
      kind: "series",
      sonarr_series_id: 101,
      title: "Better Call Saul",
      tracked: false,
      seasons: [
        {
          id: 21,
          kind: "season",
          season_number: 1,
          tracked: false,
          episodes: [
            {
              id: 32,
              kind: "episode",
              sonarr_episode_id: 302,
              season_number: 1,
              episode_number: 1,
              title: "Uno",
              has_file: true,
              tracked: false,
              current_default_track: null,
              file_id: null,
            },
          ],
        },
      ],
    },
  ],
};

/**
 * A single-Series tree with a *mixed-within-branch* resolved Tracked state
 * (COL-104 must-fix): unlike `mixedTrackedTree` above, which only ever flips
 * Tracked homogeneously across an entire branch, this Series itself resolves
 * Tracked and has two Seasons that disagree with each other -- Season 1
 * resolves Not Tracked despite having a Tracked Episode underneath it
 * (per-node Tracked overrides legitimately produce exactly this split, see
 * COL-101), while Season 2 resolves Tracked throughout. This is the shape
 * that exposed the bug where the Tracked filter reused search's
 * ancestor-context machinery: under `trackedFilter="tracked"`, the Series'
 * own match used to leak an `ancestorSearchSatisfied` flag down that made
 * Season 1 render solely because its Episode matched, even though Season 1
 * itself resolves Not Tracked. AC2 grants no such carve-out for the Tracked
 * filter (only search gets one, per AC1) -- so Season 1 must stay hidden
 * here, and Season 2 (which itself resolves Tracked) must still show.
 */
const withinBranchMixedTrackedTree: LibraryTreeResponse = {
  instance_id: 1,
  series: [
    {
      id: 12,
      kind: "series",
      sonarr_series_id: 102,
      title: "The Wire",
      tracked: true,
      seasons: [
        {
          id: 22,
          kind: "season",
          season_number: 1,
          tracked: false,
          episodes: [
            {
              id: 33,
              kind: "episode",
              sonarr_episode_id: 330,
              season_number: 1,
              episode_number: 1,
              title: "The Target",
              has_file: true,
              tracked: true,
              current_default_track: null,
              file_id: null,
            },
            {
              id: 34,
              kind: "episode",
              sonarr_episode_id: 331,
              season_number: 1,
              episode_number: 2,
              title: "The Detail",
              has_file: true,
              tracked: false,
              current_default_track: null,
              file_id: null,
            },
          ],
        },
        {
          id: 23,
          kind: "season",
          season_number: 2,
          tracked: true,
          episodes: [
            {
              id: 35,
              kind: "episode",
              sonarr_episode_id: 332,
              season_number: 2,
              episode_number: 1,
              title: "Ebb Tide",
              has_file: true,
              tracked: true,
              current_default_track: null,
              file_id: null,
            },
          ],
        },
      ],
    },
  ],
};

const movieTree: MovieLibraryTreeResponse = {
  instance_id: 2,
  movies: [
    {
      id: 40,
      kind: "movie",
      radarr_movie_id: 400,
      title: "Interstellar",
      has_file: true,
      tracked: true,
      current_default_track: null,
      file_id: null,
    },
    {
      id: 41,
      kind: "movie",
      radarr_movie_id: 401,
      title: "Dune: Part Two",
      has_file: false,
      tracked: true,
      current_default_track: null,
      file_id: null,
    },
  ],
};

/** A flat Movie list with mixed resolved Tracked values (COL-104), for the Radarr-side Tracked filter test. */
const mixedTrackedMovieTree: MovieLibraryTreeResponse = {
  instance_id: 2,
  movies: [
    {
      id: 40,
      kind: "movie",
      radarr_movie_id: 400,
      title: "Interstellar",
      has_file: true,
      tracked: true,
      current_default_track: null,
      file_id: null,
    },
    {
      id: 41,
      kind: "movie",
      radarr_movie_id: 401,
      title: "Dune: Part Two",
      has_file: false,
      tracked: false,
      current_default_track: null,
      file_id: null,
    },
  ],
};

function mockLibraryApi({
  instances,
  tree,
}: {
  instances: ArrInstance[];
  tree: LibraryTreeResponse | MovieLibraryTreeResponse;
}) {
  return vi.fn(async (url: string) => {
    if (url === "/api/instances") return jsonResponse(instances);
    if (/^\/api\/library\/instances\/\d+\/tree$/.test(url)) return jsonResponse(tree);
    throw new Error(`Unhandled request in test mock: GET ${url}`);
  });
}

interface FetchCall {
  url: string;
  init?: RequestInit;
}

/**
 * Routes GET tree/instances requests to canned responses (mutable, so a
 * toggle test can swap the tree response mid-test to simulate the
 * server-side cascade) and POST /api/library/tracked to a canned
 * `{updated: [...]}` ack -- recording every call so a test can assert the
 * request body/order.
 */
function mockLibraryApiWithToggle({
  instances,
  tree,
}: {
  instances: ArrInstance[];
  tree: LibraryTreeResponse | MovieLibraryTreeResponse;
}): { fetchMock: ReturnType<typeof vi.fn>; calls: FetchCall[]; setTree: (next: typeof tree) => void } {
  let currentTree = tree;
  const calls: FetchCall[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url === "/api/instances") return jsonResponse(instances);
    if (/^\/api\/library\/instances\/\d+\/tree$/.test(url)) return jsonResponse(currentTree);
    if (url === "/api/library/tracked" && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { references: { node_id: number; node_type: string }[]; tracked: boolean };
      return jsonResponse({
        updated: body.references.map((r) => ({ id: r.node_id, kind: r.node_type, tracked: body.tracked })),
      });
    }
    throw new Error(`Unhandled request in test mock: ${String(init?.method ?? "GET")} ${url}`);
  });
  return {
    fetchMock,
    calls,
    setTree: (next) => {
      currentTree = next;
    },
  };
}

/**
 * Routes GET tree/instances requests to canned responses (mutable, same as
 * `mockLibraryApiWithToggle`) and POST
 * `/api/jobs/trigger-default-audio/bulk` (COL-156/COL-158) to a canned
 * `{results: [...]}` ack -- one enqueued result per resolved reference,
 * recording every call so a test can assert the request body.
 */
function mockLibraryApiWithBulkDefaultAudio({
  instances,
  tree,
}: {
  instances: ArrInstance[];
  tree: LibraryTreeResponse | MovieLibraryTreeResponse;
}): { fetchMock: ReturnType<typeof vi.fn>; calls: FetchCall[]; setTree: (next: typeof tree) => void } {
  let currentTree = tree;
  const calls: FetchCall[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url === "/api/instances") return jsonResponse(instances);
    if (/^\/api\/library\/instances\/\d+\/tree$/.test(url)) return jsonResponse(currentTree);
    if (url === "/api/jobs/trigger-default-audio/bulk" && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as {
        references: { node_id: number; node_type: string }[];
      };
      return jsonResponse({
        results: body.references.map((reference) => ({
          file_path: `/media/${reference.node_type}-${reference.node_id}.mkv`,
          enqueued: true,
          job: {
            id: `job-${reference.node_id}`,
            file_path: `/media/${reference.node_type}-${reference.node_id}.mkv`,
            status: "pending",
          },
        })),
      });
    }
    throw new Error(`Unhandled request in test mock: ${String(init?.method ?? "GET")} ${url}`);
  });
  return {
    fetchMock,
    calls,
    setTree: (next) => {
      currentTree = next;
    },
  };
}

describe("LibraryPage (COL-100)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a Sonarr instance's Series > Season > Episode tree with working expand/collapse", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: seriesTree }));
    renderLibraryPage(1);

    const seriesToggle = await screen.findByRole("button", { name: /breaking bad/i });
    expect(screen.queryByText(/season 1/i)).not.toBeInTheDocument();

    fireEvent.click(seriesToggle);
    const seasonToggle = await screen.findByRole("button", { name: /season 1/i });
    expect(screen.queryByText(/pilot/i)).not.toBeInTheDocument();

    fireEvent.click(seasonToggle);
    expect(await screen.findByText(/pilot/i)).toBeInTheDocument();
    expect(screen.getByText(/cat's in the bag/i)).toBeInTheDocument();

    // Collapsing the season hides its episodes again without touching the series.
    fireEvent.click(seasonToggle);
    expect(screen.queryByText(/pilot/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /season 1/i })).toBeInTheDocument();
  });

  it("visually distinguishes episodes with no file from episodes with a file", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: seriesTree }));
    renderLibraryPage(1);

    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));

    const pilotRow = (await screen.findByText(/pilot/i)).closest("tr") as HTMLElement;
    const missingRow = screen.getByText(/cat's in the bag/i).closest("tr") as HTMLElement;

    expect(within(pilotRow).getByText("Has file")).toBeInTheDocument();
    expect(pilotRow.className).not.toContain("library-tree-table__row--dimmed");

    expect(within(missingRow).getByText("Missing")).toBeInTheDocument();
    expect(missingRow.className).toContain("library-tree-table__row--dimmed");
  });

  it("links an Episode row with a resolved tracked-file id to its file detail page (COL-198)", async () => {
    const treeWithFileId: LibraryTreeResponse = {
      ...seriesTree,
      series: [
        {
          ...seriesTree.series[0],
          seasons: [
            {
              ...seriesTree.series[0].seasons[0],
              episodes: [
                { ...seriesTree.series[0].seasons[0].episodes[0], file_id: 900 },
                seriesTree.series[0].seasons[0].episodes[1], // has_file: false, file_id: null
              ],
            },
          ],
        },
      ],
    };
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: treeWithFileId }));
    renderLibraryPage(1);

    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));

    // "Pilot" has a resolved file_id (900) -- same route WantedPage links to
    // for the same file (`/wanted/:fileId`, matched against `WantedFile.id`).
    const pilotLink = await screen.findByRole("link", { name: /pilot/i });
    expect(pilotLink).toHaveAttribute("href", "/wanted/900");

    // "Cat's in the Bag..." has no file at all (has_file: false, file_id:
    // null) -- unaffected, no link.
    const missingRow = screen.getByText(/cat's in the bag/i).closest("tr") as HTMLElement;
    expect(within(missingRow).queryByRole("link")).not.toBeInTheDocument();
  });

  it("does not link an Episode row that has a file but no resolved tracked-file id yet (COL-198)", async () => {
    // has_file: true but file_id: null -- e.g. no bridged tracked-media row
    // yet. Matches `seriesTree`'s "Pilot" fixture, which is exactly this
    // state (has_file: true, file_id: null).
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: seriesTree }));
    renderLibraryPage(1);

    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));

    const pilotRow = (await screen.findByText(/pilot/i)).closest("tr") as HTMLElement;
    expect(within(pilotRow).queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders the current Default Audio Track column, with a clear unknown state (COL-154)", async () => {
    const treeWithDefaultTrack: LibraryTreeResponse = {
      ...seriesTree,
      series: [
        {
          ...seriesTree.series[0],
          seasons: [
            {
              ...seriesTree.series[0].seasons[0],
              episodes: [
                {
                  ...seriesTree.series[0].seasons[0].episodes[0],
                  current_default_track: { language: "dan", channel_layout: "5.1" },
                },
                seriesTree.series[0].seasons[0].episodes[1], // current_default_track: null
              ],
            },
          ],
        },
      ],
    };
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: treeWithDefaultTrack }));
    renderLibraryPage(1);

    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));

    const pilotRow = (await screen.findByText(/pilot/i)).closest("tr") as HTMLElement;
    expect(within(pilotRow).getByText("Dan · 5.1")).toBeInTheDocument();

    const unprobedRow = screen.getByText(/cat's in the bag/i).closest("tr") as HTMLElement;
    expect(within(unprobedRow).getByText("Unknown")).toBeInTheDocument();
  });

  it("renders the current Default Audio Track column for a flat Movie list (COL-154)", async () => {
    const movieTreeWithDefaultTrack: MovieLibraryTreeResponse = {
      ...movieTree,
      movies: [
        { ...movieTree.movies[0], current_default_track: { language: "eng", channel_layout: "5.1" } },
        movieTree.movies[1], // current_default_track: null
      ],
    };
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [radarrInstance], tree: movieTreeWithDefaultTrack }));
    renderLibraryPage(2);

    const interstellarRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
    expect(within(interstellarRow).getByText("Eng · 5.1")).toBeInTheDocument();

    const duneRow = screen.getByText("Dune: Part Two").closest("tr") as HTMLElement;
    expect(within(duneRow).getByText("Unknown")).toBeInTheDocument();
  });

  it("renders a Radarr instance's flat Movie list with no season/episode nesting", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [radarrInstance], tree: movieTree }));
    renderLibraryPage(2);

    expect(await screen.findByText("Interstellar")).toBeInTheDocument();
    expect(screen.getByText("Dune: Part Two")).toBeInTheDocument();
    expect(screen.queryByText(/season/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /interstellar/i })).not.toBeInTheDocument();

    const presentRow = screen.getByText("Interstellar").closest("tr") as HTMLElement;
    const missingRow = screen.getByText("Dune: Part Two").closest("tr") as HTMLElement;
    expect(within(presentRow).getByText("Has file")).toBeInTheDocument();
    expect(presentRow.className).not.toContain("library-tree-table__row--dimmed");
    expect(within(missingRow).getByText("Missing")).toBeInTheDocument();
    expect(missingRow.className).toContain("library-tree-table__row--dimmed");
  });

  it("links a Movie row with a resolved tracked-file id to its file detail page, leaving a fileless row unaffected (COL-198)", async () => {
    const movieTreeWithFileId: MovieLibraryTreeResponse = {
      ...movieTree,
      movies: [
        { ...movieTree.movies[0], file_id: 901 },
        movieTree.movies[1], // has_file: false, file_id: null
      ],
    };
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [radarrInstance], tree: movieTreeWithFileId }));
    renderLibraryPage(2);

    // "Interstellar" has a resolved file_id (901) -- same route WantedPage
    // links to for the same file.
    const interstellarLink = await screen.findByRole("link", { name: "Interstellar" });
    expect(interstellarLink).toHaveAttribute("href", "/wanted/901");

    // "Dune: Part Two" has no file at all -- unaffected, no link.
    const duneRow = screen.getByText("Dune: Part Two").closest("tr") as HTMLElement;
    expect(within(duneRow).queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders an error state when the tree request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/instances") return jsonResponse([sonarrInstance]);
        return jsonResponse({ detail: "No arr instance with id=1" }, 404);
      }),
    );
    renderLibraryPage(1);

    expect(
      await screen.findByText(/couldn't load this library: no arr instance with id=1/i),
    ).toBeInTheDocument();
  });

  it("renders an empty state when a Sonarr library has no series yet", async () => {
    vi.stubGlobal(
      "fetch",
      mockLibraryApi({ instances: [sonarrInstance], tree: { instance_id: 1, series: [] } }),
    );
    renderLibraryPage(1);

    expect(await screen.findByText(/no series found in this library yet/i)).toBeInTheDocument();
  });

  it("toggling a single Episode row's Tracked control updates only that node (COL-101)", async () => {
    const { fetchMock, calls, setTree } = mockLibraryApiWithToggle({
      instances: [sonarrInstance],
      tree: seriesTree,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibraryPage(1);

    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    const pilotRow = (await screen.findByText(/pilot/i)).closest("tr") as HTMLElement;

    // Server-side response to the refetch this toggle triggers: only the
    // Pilot episode flips, everything else (series, season, sibling
    // episode) stays Tracked.
    const afterToggle: LibraryTreeResponse = {
      ...seriesTree,
      series: [
        {
          ...seriesTree.series[0],
          seasons: [
            {
              ...seriesTree.series[0].seasons[0],
              episodes: [
                { ...seriesTree.series[0].seasons[0].episodes[0], tracked: false },
                seriesTree.series[0].seasons[0].episodes[1],
              ],
            },
          ],
        },
      ],
    };
    setTree(afterToggle);

    fireEvent.click(within(pilotRow).getByRole("button", { name: /tracked/i }));

    const trackedCall = await vi.waitFor(() => {
      const match = calls.find((call) => call.url === "/api/library/tracked");
      if (!match) throw new Error("not yet called");
      return match;
    });
    expect(trackedCall.init?.method).toBe("POST");
    expect(JSON.parse(String(trackedCall.init?.body))).toEqual({
      references: [{ node_type: "episode", node_id: 30 }],
      tracked: false,
    });

    // Refetch lands: this row now reads Not Tracked, its sibling is untouched.
    const updatedPilotRow = (await screen.findByText(/pilot/i)).closest("tr") as HTMLElement;
    expect(within(updatedPilotRow).getByRole("button", { name: /^not tracked$/i })).toBeInTheDocument();
    const siblingRow = screen.getByText(/cat's in the bag/i).closest("tr") as HTMLElement;
    expect(within(siblingRow).getByRole("button", { name: /^tracked$/i })).toBeInTheDocument();
  });

  it("toggling a Series row cascades to its Season/Episode descendants, visible without a manual refresh (COL-101)", async () => {
    const { fetchMock, calls, setTree } = mockLibraryApiWithToggle({
      instances: [sonarrInstance],
      tree: seriesTree,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibraryPage(1);

    const seriesRow = (await screen.findByRole("button", { name: /breaking bad/i })).closest(
      "tr",
    ) as HTMLElement;

    // The server-side cascade: every descendant flips to Not Tracked too.
    const afterCascade: LibraryTreeResponse = {
      instance_id: 1,
      series: [
        {
          ...seriesTree.series[0],
          tracked: false,
          seasons: seriesTree.series[0].seasons.map((season) => ({
            ...season,
            tracked: false,
            episodes: season.episodes.map((episode) => ({ ...episode, tracked: false })),
          })),
        },
      ],
    };
    setTree(afterCascade);

    fireEvent.click(within(seriesRow).getByRole("button", { name: /tracked/i }));

    const trackedCall = await vi.waitFor(() => {
      const match = calls.find((call) => call.url === "/api/library/tracked");
      if (!match) throw new Error("not yet called");
      return match;
    });
    expect(JSON.parse(String(trackedCall.init?.body))).toEqual({
      references: [{ node_type: "series", node_id: 10 }],
      tracked: false,
    });

    const updatedSeriesRow = (
      await screen.findByRole("button", { name: /breaking bad/i })
    ).closest("tr") as HTMLElement;
    expect(
      within(updatedSeriesRow).getByRole("button", { name: /^not tracked$/i }),
    ).toBeInTheDocument();

    // Expand down to the episodes: the cascade landed on every descendant too.
    fireEvent.click(screen.getByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    const pilotRow = (await screen.findByText(/pilot/i)).closest("tr") as HTMLElement;
    expect(within(pilotRow).getByRole("button", { name: /^not tracked$/i })).toBeInTheDocument();
  });

  it("toggling a Movie row's Tracked control calls the endpoint with a movie reference (COL-101)", async () => {
    const { fetchMock, calls, setTree } = mockLibraryApiWithToggle({
      instances: [radarrInstance],
      tree: movieTree,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibraryPage(2);

    const interstellarRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;

    setTree({
      ...movieTree,
      movies: [{ ...movieTree.movies[0], tracked: false }, movieTree.movies[1]],
    });

    fireEvent.click(within(interstellarRow).getByRole("button", { name: /tracked/i }));

    const trackedCall = await vi.waitFor(() => {
      const match = calls.find((call) => call.url === "/api/library/tracked");
      if (!match) throw new Error("not yet called");
      return match;
    });
    expect(JSON.parse(String(trackedCall.init?.body))).toEqual({
      references: [{ node_type: "movie", node_id: 40 }],
      tracked: false,
    });

    const updatedRow = (await screen.findByText("Interstellar")).closest("tr") as HTMLElement;
    expect(within(updatedRow).getByRole("button", { name: /^not tracked$/i })).toBeInTheDocument();
  });

  it("shows an inline error and re-enables the toggle when the update request fails", async () => {
    const { calls } = mockLibraryApiWithToggle({ instances: [sonarrInstance], tree: seriesTree });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, init });
        if (url === "/api/instances") return jsonResponse([sonarrInstance]);
        if (/^\/api\/library\/instances\/\d+\/tree$/.test(url)) return jsonResponse(seriesTree);
        if (url === "/api/library/tracked") return jsonResponse({ detail: "boom" }, 500);
        throw new Error(`Unhandled request in test mock: GET ${url}`);
      }),
    );
    renderLibraryPage(1);

    const seriesRow = (await screen.findByRole("button", { name: /breaking bad/i })).closest(
      "tr",
    ) as HTMLElement;
    fireEvent.click(within(seriesRow).getByRole("button", { name: /tracked/i }));

    expect(await screen.findByText(/couldn't update tracked: boom/i)).toBeInTheDocument();
    // Row still reads its original (unchanged) Tracked state -- the failed
    // request never got a chance to flip it via a refetch.
    expect(within(seriesRow).getByRole("button", { name: /^tracked$/i })).not.toBeDisabled();
  });

  it("shows the bulk-action toolbar only while at least one row is selected (COL-103)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: seriesTree }));
    renderLibraryPage(1);

    expect(screen.queryByRole("toolbar", { name: /bulk library actions/i })).not.toBeInTheDocument();

    const seriesCheckbox = await screen.findByRole("checkbox", { name: /select breaking bad$/i });
    fireEvent.click(seriesCheckbox);

    expect(await screen.findByRole("toolbar", { name: /bulk library actions/i })).toBeInTheDocument();
    expect(screen.getByText(/1 row selected/i)).toBeInTheDocument();

    fireEvent.click(seriesCheckbox);

    expect(screen.queryByRole("toolbar", { name: /bulk library actions/i })).not.toBeInTheDocument();
  });

  it("retains checkbox selections across different series/seasons/levels through expand/collapse (COL-103)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: twoSeriesTree }));
    renderLibraryPage(1);

    // Select the second series' row without expanding it.
    fireEvent.click(await screen.findByRole("checkbox", { name: /select better call saul$/i }));

    // Expand the first series down to its episodes and select one there too
    // -- a cross-branch, cross-level selection (Series + Episode).
    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /select breaking bad s1e1 pilot/i }));

    expect(await screen.findByText(/2 rows selected/i)).toBeInTheDocument();

    // Collapse the season and the series, then re-expand both -- the
    // episode checkbox must still read checked, since the selection lives
    // in LibraryPage, not the collapsed subtree's own local expand state.
    fireEvent.click(screen.getByRole("button", { name: /season 1/i }));
    fireEvent.click(screen.getByRole("button", { name: /breaking bad/i }));
    expect(screen.getByText(/2 rows selected/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    expect(
      await screen.findByRole("checkbox", { name: /select breaking bad s1e1 pilot/i }),
    ).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /select better call saul$/i })).toBeChecked();
  });

  it("applies a bulk action to the whole mixed-level selection in one request and reflects the result, including cascade, without a manual refresh (COL-103)", async () => {
    const { fetchMock, calls, setTree } = mockLibraryApiWithToggle({
      instances: [sonarrInstance],
      tree: twoSeriesTree,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderLibraryPage(1);

    // Cross-branch, cross-level selection: the "Better Call Saul" series row
    // plus "Breaking Bad"'s sibling episode "Cat's in the Bag...".
    fireEvent.click(await screen.findByRole("checkbox", { name: /select better call saul$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /select breaking bad s1e2/i }));

    expect(await screen.findByText(/2 rows selected/i)).toBeInTheDocument();

    // Server-side result of the bulk apply: the directly-selected episode
    // flips, and the selected series' cascade flips its own season/episode.
    const afterBulk: LibraryTreeResponse = {
      instance_id: 1,
      series: [
        {
          ...twoSeriesTree.series[0],
          seasons: [
            {
              ...twoSeriesTree.series[0].seasons[0],
              episodes: [
                twoSeriesTree.series[0].seasons[0].episodes[0],
                { ...twoSeriesTree.series[0].seasons[0].episodes[1], tracked: false },
              ],
            },
          ],
        },
        {
          ...twoSeriesTree.series[1],
          tracked: false,
          seasons: twoSeriesTree.series[1].seasons.map((season) => ({
            ...season,
            tracked: false,
            episodes: season.episodes.map((episode) => ({ ...episode, tracked: false })),
          })),
        },
      ],
    };
    setTree(afterBulk);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /mark not tracked/i }));
    });

    const bulkCall = await vi.waitFor(() => {
      const match = calls.find((call) => call.url === "/api/library/tracked");
      if (!match) throw new Error("not yet called");
      return match;
    });
    expect(bulkCall.init?.method).toBe("POST");
    const body = JSON.parse(String(bulkCall.init?.body)) as {
      references: { node_type: string; node_id: number }[];
      tracked: boolean;
    };
    expect(body.tracked).toBe(false);
    expect(body.references).toHaveLength(2);
    expect(body.references).toEqual(
      expect.arrayContaining([
        { node_type: "series", node_id: 11 },
        { node_type: "episode", node_id: 31 },
      ]),
    );

    // Toolbar disappears -- selection cleared after a successful apply.
    await vi.waitFor(() => {
      expect(screen.queryByRole("toolbar", { name: /bulk library actions/i })).not.toBeInTheDocument();
    });

    // Refetch landed without a manual refresh: the directly-selected sibling
    // episode is Not Tracked...
    const siblingRow = (await screen.findByText(/cat's in the bag/i)).closest("tr") as HTMLElement;
    expect(within(siblingRow).getByRole("button", { name: /^not tracked$/i })).toBeInTheDocument();

    // ...and the selected series' cascade to its own descendants landed too.
    fireEvent.click(screen.getByRole("button", { name: /breaking bad/i })); // collapse, avoids a duplicate "Season 1" label
    fireEvent.click(screen.getByRole("button", { name: /better call saul/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    const unoRow = (await screen.findByText(/uno/i)).closest("tr") as HTMLElement;
    expect(within(unoRow).getByRole("button", { name: /^not tracked$/i })).toBeInTheDocument();
  });

  it("shows a 'Set Default Audio Track' bulk action alongside the Tracked actions once a row is selected (COL-158)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: seriesTree }));
    renderLibraryPage(1);

    expect(screen.queryByRole("button", { name: /set default audio track/i })).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("checkbox", { name: /select breaking bad$/i }));

    expect(await screen.findByRole("button", { name: /set default audio track/i })).toBeInTheDocument();
  });

  it("applies the bulk Set Default Audio Track action to the whole mixed-level selection in one request and honestly summarizes the trigger's own enqueued/already-correct result (COL-158)", async () => {
    // Deliberately does NOT swap the mocked tree to an "after" state before
    // clicking: `SET_DEFAULT_AUDIO` jobs run asynchronously (probe -> remux
    // -> re-probe), well after the trigger request returns, so a genuinely
    // honest test can't pretend the Default Audio column already reflects
    // the job's outcome. What the trigger response *does* know synchronously
    // is, per resolved file, whether a job was enqueued or the file was
    // already correct -- that's what this test asserts gets surfaced.
    const calls: FetchCall[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, init });
        if (url === "/api/instances") return jsonResponse([sonarrInstance]);
        if (/^\/api\/library\/instances\/\d+\/tree$/.test(url)) return jsonResponse(twoSeriesTree);
        if (url === "/api/jobs/trigger-default-audio/bulk" && init?.method === "POST") {
          return jsonResponse({
            results: [
              {
                file_path: "/media/better-call-saul-s1e1.mkv",
                enqueued: true,
                job: { id: "job-1", file_path: "/media/better-call-saul-s1e1.mkv", status: "pending" },
              },
              {
                file_path: "/media/breaking-bad-s1e2.mkv",
                enqueued: false,
                job: null,
              },
            ],
          });
        }
        throw new Error(`Unhandled request in test mock: ${String(init?.method ?? "GET")} ${url}`);
      }),
    );
    renderLibraryPage(1);

    // Same cross-branch, cross-level selection shape as the bulk Tracked
    // test: the "Better Call Saul" series row plus "Breaking Bad"'s sibling
    // episode "Cat's in the Bag...".
    fireEvent.click(await screen.findByRole("checkbox", { name: /select better call saul$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /select breaking bad s1e2/i }));

    expect(await screen.findByText(/2 rows selected/i)).toBeInTheDocument();

    // The sibling episode's Default Audio column reads its real,
    // still-unknown value going in.
    const siblingRowBefore = (await screen.findByText(/cat's in the bag/i)).closest("tr") as HTMLElement;
    expect(within(siblingRowBefore).getByText("Unknown")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /set default audio track/i }));
    });

    const bulkCall = await vi.waitFor(() => {
      const match = calls.find((call) => call.url === "/api/jobs/trigger-default-audio/bulk");
      if (!match) throw new Error("not yet called");
      return match;
    });
    expect(bulkCall.init?.method).toBe("POST");
    const body = JSON.parse(String(bulkCall.init?.body)) as {
      references: { node_type: string; node_id: number }[];
    };
    expect(body.references).toHaveLength(2);
    expect(body.references).toEqual(
      expect.arrayContaining([
        { node_type: "series", node_id: 11 },
        { node_type: "episode", node_id: 31 },
      ]),
    );

    // Toolbar disappears -- selection cleared after a successful apply.
    await vi.waitFor(() => {
      expect(screen.queryByRole("toolbar", { name: /bulk library actions/i })).not.toBeInTheDocument();
    });

    // The trigger response's real, synchronously-known result is surfaced --
    // one job enqueued, one file already correct/skipped -- not a fabricated
    // claim that the column values already updated.
    expect(
      await screen.findByText(/1 job enqueued, 1 already correct or skipped/i),
    ).toBeInTheDocument();

    // The refetch landed (the tree endpoint was hit again after the trigger)
    // but since the mocked tree never changes, the sibling episode's Default
    // Audio column still honestly reads its real, not-yet-updated value --
    // the enqueued job hasn't run, so there is nothing new to show yet.
    const treeCallCount = calls.filter((call) =>
      /^\/api\/library\/instances\/\d+\/tree$/.test(call.url),
    ).length;
    expect(treeCallCount).toBeGreaterThanOrEqual(2);
    const siblingRowAfter = (await screen.findByText(/cat's in the bag/i)).closest("tr") as HTMLElement;
    expect(within(siblingRowAfter).getByText("Unknown")).toBeInTheDocument();
  });

  it("shows an inline error, distinct from the Tracked error, when the bulk Set Default Audio Track request fails (COL-158)", async () => {
    const { calls } = mockLibraryApiWithBulkDefaultAudio({
      instances: [sonarrInstance],
      tree: seriesTree,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, init });
        if (url === "/api/instances") return jsonResponse([sonarrInstance]);
        if (/^\/api\/library\/instances\/\d+\/tree$/.test(url)) return jsonResponse(seriesTree);
        if (url === "/api/jobs/trigger-default-audio/bulk") return jsonResponse({ detail: "boom" }, 500);
        throw new Error(`Unhandled request in test mock: GET ${url}`);
      }),
    );
    renderLibraryPage(1);

    fireEvent.click(await screen.findByRole("checkbox", { name: /select breaking bad$/i }));
    fireEvent.click(await screen.findByRole("button", { name: /set default audio track/i }));

    expect(
      await screen.findByText(/couldn't trigger set default audio track: boom/i),
    ).toBeInTheDocument();
    // The unrelated Tracked error never fires alongside it.
    expect(screen.queryByText(/couldn't update tracked/i)).not.toBeInTheDocument();
    // Selection is retained (not cleared) after a failed apply, same as a
    // failed bulk Tracked apply -- the user can retry as-is.
    expect(await screen.findByText(/1 row selected/i)).toBeInTheDocument();
  });

  it("narrows to the Series matching the search query and shows its whole subtree (COL-104)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: twoSeriesTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), {
      target: { value: "Call Saul" },
    });

    // The unmatched series is gone entirely...
    expect(screen.queryByRole("button", { name: /breaking bad/i })).not.toBeInTheDocument();
    // ...and the matched series' whole subtree is shown, already expanded
    // (COL-104: a search match shouldn't hide behind a collapsed toggle).
    expect(await screen.findByRole("button", { name: /better call saul/i })).toBeInTheDocument();
    expect(screen.getByText(/season 1/i)).toBeInTheDocument();
    expect(screen.getByText(/uno/i)).toBeInTheDocument();
  });

  it("finding a matching Episode keeps its ancestor Series/Season for context, without pulling in unrelated siblings (COL-104)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: twoSeriesTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), {
      target: { value: "Cat's in the Bag" },
    });

    // Ancestor context for the match, auto-expanded.
    expect(await screen.findByRole("button", { name: /breaking bad/i })).toBeInTheDocument();
    expect(screen.getByText(/cat's in the bag/i)).toBeInTheDocument();

    // The matching Episode's own sibling and the unrelated second Series
    // don't come along for the ride.
    expect(screen.queryByText(/^pilot$/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /better call saul/i })).not.toBeInTheDocument();
  });

  it("shows a no-matches message when the search query matches nothing", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: twoSeriesTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), {
      target: { value: "no such title anywhere" },
    });

    expect(await screen.findByText(/no series match your search or filter/i)).toBeInTheDocument();
  });

  it("narrows to only rows whose resolved Tracked value matches the selected Tracked filter (COL-104)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: mixedTrackedTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /breaking bad/i });
    const trackedFilterSelect = screen.getByRole("combobox", { name: /tracked filter/i });

    // "Not Tracked" narrows to the series that resolves Not Tracked...
    fireEvent.change(trackedFilterSelect, { target: { value: "not-tracked" } });
    expect(await screen.findByRole("button", { name: /better call saul/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^breaking bad$/i })).not.toBeInTheDocument();

    // ...and "Tracked" narrows to the one that resolves Tracked instead.
    fireEvent.change(trackedFilterSelect, { target: { value: "tracked" } });
    expect(await screen.findByRole("button", { name: /breaking bad/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /better call saul/i })).not.toBeInTheDocument();

    // "All" (the default) shows everything again.
    fireEvent.change(trackedFilterSelect, { target: { value: "all" } });
    expect(await screen.findByRole("button", { name: /breaking bad/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /better call saul/i })).toBeInTheDocument();
  });

  it("narrows a flat Movie list to only rows whose resolved Tracked value matches the selected Tracked filter (COL-104)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [radarrInstance], tree: mixedTrackedMovieTree }));
    renderLibraryPage(2);

    await screen.findByText("Interstellar");
    fireEvent.change(screen.getByRole("combobox", { name: /tracked filter/i }), {
      target: { value: "not-tracked" },
    });

    expect(await screen.findByText("Dune: Part Two")).toBeInTheDocument();
    expect(screen.queryByText("Interstellar")).not.toBeInTheDocument();
  });

  it("composes search and the Tracked filter together (AND), narrowing correctly in combination (COL-104)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: mixedTrackedTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), {
      target: { value: "Bad" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /tracked filter/i }), {
      target: { value: "not-tracked" },
    });

    // "Bad" only matches the series that resolves Tracked, so combined with
    // the Not-Tracked filter neither series has anything left to show.
    expect(await screen.findByText(/no series match your search or filter/i)).toBeInTheDocument();

    // Switching the filter to "Tracked" keeps the same search query and now
    // matches the "Breaking Bad" series again.
    fireEvent.change(screen.getByRole("combobox", { name: /tracked filter/i }), {
      target: { value: "tracked" },
    });
    expect(await screen.findByRole("button", { name: /breaking bad/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /better call saul/i })).not.toBeInTheDocument();
  });

  it("keeps the Tracked filter strict per-node within a single branch, with no ancestor-context carve-out (COL-104 must-fix)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: withinBranchMixedTrackedTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /the wire/i });
    fireEvent.change(screen.getByRole("combobox", { name: /tracked filter/i }), {
      target: { value: "tracked" },
    });

    // The Series itself resolves Tracked, so it still shows (the inverse
    // case: a node that itself matches the filter must keep showing).
    expect(await screen.findByRole("button", { name: /the wire/i })).toBeInTheDocument();

    // Season 2 itself resolves Tracked, so it (and its Tracked Episode) show.
    expect(await screen.findByRole("button", { name: /season 2/i })).toBeInTheDocument();
    expect(screen.getByText(/ebb tide/i)).toBeInTheDocument();

    // Season 1 itself resolves Not Tracked -- it must NOT render even though
    // "The Target" underneath it resolves Tracked. Under the old
    // ancestor-context-leaking implementation this row (and "The Target")
    // would incorrectly appear because the Series' own match satisfied the
    // shared `ancestorSearchSatisfied`-style flag; AC2 grants the Tracked
    // filter no such carve-out.
    expect(screen.queryByRole("button", { name: /season 1/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/the target/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/the detail/i)).not.toBeInTheDocument();
  });

  it("'Select all' adds every currently-rendered Series/Season/Episode node to the selection (COL-109)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: twoSeriesTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.click(screen.getByRole("button", { name: /select all/i }));

    // twoSeriesTree: "Breaking Bad" (series + season + 2 episodes) plus
    // "Better Call Saul" (series + season + 1 episode) = 7 rows total, even
    // though neither series is expanded (COL-109: "currently-rendered" means
    // "post filter", not "currently visible in the DOM").
    expect(await screen.findByText(/7 rows selected/i)).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: /breaking bad/i }));
    expect(await screen.findByRole("checkbox", { name: /select breaking bad$/i })).toBeChecked();
    fireEvent.click(await screen.findByRole("button", { name: /season 1/i }));
    expect(
      await screen.findByRole("checkbox", { name: /select breaking bad s1e1 pilot/i }),
    ).toBeChecked();
  });

  it("'Select all' composes with an existing selection and survives a later filter change (COL-109)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: twoSeriesTree }));
    renderLibraryPage(1);

    // A row selected before "Select all" is run...
    fireEvent.click(await screen.findByRole("checkbox", { name: /select better call saul$/i }));
    expect(await screen.findByText(/1 row selected/i)).toBeInTheDocument();

    // Narrow to just "Breaking Bad" via search, then select all of what's
    // currently shown -- "Better Call Saul"'s prior selection must survive
    // even though it's not part of this "Select all" pass.
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), {
      target: { value: "Breaking Bad" },
    });
    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.click(screen.getByRole("button", { name: /select all/i }));

    // "Breaking Bad" + its 1 season + its 2 episodes (4) plus the
    // pre-existing "Better Call Saul" selection (1) = 5.
    expect(await screen.findByText(/5 rows selected/i)).toBeInTheDocument();

    // Clearing the search doesn't retroactively prune anything either.
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), { target: { value: "" } });
    expect(await screen.findByText(/5 rows selected/i)).toBeInTheDocument();
    expect(
      await screen.findByRole("checkbox", { name: /select better call saul$/i }),
    ).toBeChecked();
  });

  it("'Select all' selection also survives changing the Tracked filter afterward (COL-109)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: mixedTrackedTree }));
    renderLibraryPage(1);

    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.change(screen.getByRole("combobox", { name: /tracked filter/i }), {
      target: { value: "tracked" },
    });
    await screen.findByRole("button", { name: /breaking bad/i });
    fireEvent.click(screen.getByRole("button", { name: /select all/i }));

    // "Breaking Bad" is the only series resolving Tracked here: series +
    // season + 2 episodes = 4.
    expect(await screen.findByText(/4 rows selected/i)).toBeInTheDocument();

    // Switching to "Not Tracked" hides "Breaking Bad" entirely (it resolves
    // Tracked) -- its selection must persist even while it's off-screen.
    fireEvent.change(screen.getByRole("combobox", { name: /tracked filter/i }), {
      target: { value: "not-tracked" },
    });
    await screen.findByRole("button", { name: /better call saul/i });
    expect(screen.queryByRole("button", { name: /^breaking bad$/i })).not.toBeInTheDocument();
    expect(screen.getByText(/4 rows selected/i)).toBeInTheDocument();

    // Back to "All": "Breaking Bad" reappears, still checked.
    fireEvent.change(screen.getByRole("combobox", { name: /tracked filter/i }), {
      target: { value: "all" },
    });
    expect(
      await screen.findByRole("checkbox", { name: /select breaking bad$/i }),
    ).toBeChecked();
  });

  it("'Select all' adds every currently-rendered Movie row to the selection (COL-109)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [radarrInstance], tree: movieTree }));
    renderLibraryPage(2);

    await screen.findByText("Interstellar");
    fireEvent.click(screen.getByRole("button", { name: /select all/i }));

    expect(await screen.findByText(/2 rows selected/i)).toBeInTheDocument();
    expect(await screen.findByRole("checkbox", { name: /select interstellar/i })).toBeChecked();
    expect(await screen.findByRole("checkbox", { name: /select dune: part two/i })).toBeChecked();
  });

  it("keeps a filtered-out row's cross-level selection intact rather than dropping it (COL-103/COL-104)", async () => {
    vi.stubGlobal("fetch", mockLibraryApi({ instances: [sonarrInstance], tree: twoSeriesTree }));
    renderLibraryPage(1);

    fireEvent.click(await screen.findByRole("checkbox", { name: /select breaking bad$/i }));
    expect(await screen.findByText(/1 row selected/i)).toBeInTheDocument();

    // Search narrows "Breaking Bad" out of view entirely.
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), {
      target: { value: "Call Saul" },
    });
    await screen.findByRole("button", { name: /better call saul/i });
    expect(screen.queryByRole("button", { name: /breaking bad/i })).not.toBeInTheDocument();

    // Its selection persists even while hidden.
    expect(screen.getByText(/1 row selected/i)).toBeInTheDocument();

    // Clearing the search brings the row back, still checked.
    fireEvent.change(screen.getByRole("searchbox", { name: /search titles/i }), { target: { value: "" } });
    expect(
      await screen.findByRole("checkbox", { name: /select breaking bad$/i }),
    ).toBeChecked();
  });
});
