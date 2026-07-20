import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WantedPage } from "../pages/WantedPage";
import type { WantedFile } from "../types/wanted";

/** `WantedPage` links each row to `/wanted/:fileId` (COL-34), so it needs a router. */
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
  },
  {
    id: 2,
    file_path: "/media/tv/Show/Season 01/Show.S01E01.mkv",
    missing_targets: [{ language: "fr", target: "stereo" }],
    created_at: "2026-07-03T00:00:00Z",
    updated_at: "2026-07-03T00:00:00Z",
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
});
