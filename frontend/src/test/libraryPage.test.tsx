import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LibraryPage } from "../pages/LibraryPage";
import type { LibraryTreeResponse, MovieLibraryTreeResponse } from "../types/library";
import type { ArrInstance } from "../types/instances";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

function renderLibraryPage(instanceId: number) {
  return render(
    <MemoryRouter initialEntries={[`/libraries/${instanceId}`]}>
      <Routes>
        <Route path="/libraries/:instanceId" element={<LibraryPage />} />
      </Routes>
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
});
