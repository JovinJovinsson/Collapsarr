import { fireEvent, render, screen, within } from "@testing-library/react";
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
    { id: 40, kind: "movie", radarr_movie_id: 400, title: "Interstellar", has_file: true, tracked: true },
    {
      id: 41,
      kind: "movie",
      radarr_movie_id: 401,
      title: "Dune: Part Two",
      has_file: false,
      tracked: true,
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

    expect(screen.queryByRole("toolbar", { name: /bulk tracked actions/i })).not.toBeInTheDocument();

    const seriesCheckbox = await screen.findByRole("checkbox", { name: /select breaking bad$/i });
    fireEvent.click(seriesCheckbox);

    expect(await screen.findByRole("toolbar", { name: /bulk tracked actions/i })).toBeInTheDocument();
    expect(screen.getByText(/1 row selected/i)).toBeInTheDocument();

    fireEvent.click(seriesCheckbox);

    expect(screen.queryByRole("toolbar", { name: /bulk tracked actions/i })).not.toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: /mark not tracked/i }));

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
      expect(screen.queryByRole("toolbar", { name: /bulk tracked actions/i })).not.toBeInTheDocument();
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
});
