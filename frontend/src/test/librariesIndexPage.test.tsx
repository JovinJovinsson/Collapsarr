import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { rememberVisitedLibraryInstance } from "../api/library";
import { InstancesProvider } from "../components/InstancesProvider";
import { LibrariesIndexPage } from "../pages/LibrariesIndexPage";
import type { ArrInstance } from "../types/instances";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

function mockInstancesApi(instances: ArrInstance[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/instances") return jsonResponse(instances);
      throw new Error(`Unhandled request in test mock: GET ${url}`);
    }),
  );
}

/** Stand-in for `LibraryPage` at `/libraries/:instanceId` -- just proves the redirect landed. */
function LandingMarker() {
  const { instanceId } = useParams<{ instanceId: string }>();
  return <div>Landed on instance {instanceId}</div>;
}

function renderLibrariesIndex() {
  return render(
    <MemoryRouter initialEntries={["/libraries"]}>
      <InstancesProvider>
        <Routes>
          <Route path="/libraries" element={<LibrariesIndexPage />} />
          <Route path="/libraries/:instanceId" element={<LandingMarker />} />
        </Routes>
      </InstancesProvider>
    </MemoryRouter>,
  );
}

const sonarr: ArrInstance = {
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

const radarr: ArrInstance = {
  ...sonarr,
  id: 2,
  name: "Radarr Prod",
  type: "radarr",
  base_url: "http://localhost:7878",
};

describe("LibrariesIndexPage (COL-100)", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("falls back to the first configured instance when none was previously visited", async () => {
    mockInstancesApi([sonarr, radarr]);
    renderLibrariesIndex();

    expect(await screen.findByText("Landed on instance 1")).toBeInTheDocument();
  });

  it("redirects to the last-visited instance recorded in localStorage", async () => {
    rememberVisitedLibraryInstance(2);
    mockInstancesApi([sonarr, radarr]);
    renderLibrariesIndex();

    expect(await screen.findByText("Landed on instance 2")).toBeInTheDocument();
  });

  it("falls back to the first instance when the last-visited id is no longer configured", async () => {
    rememberVisitedLibraryInstance(999);
    mockInstancesApi([sonarr, radarr]);
    renderLibrariesIndex();

    expect(await screen.findByText("Landed on instance 1")).toBeInTheDocument();
  });

  it("shows an empty state and does not redirect when no instances are configured", async () => {
    mockInstancesApi([]);
    renderLibrariesIndex();

    expect(
      await screen.findByText(/no sonarr or radarr instances configured yet/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/landed on instance/i)).not.toBeInTheDocument();
  });

  it("renders an error state when instances fail to load", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    renderLibrariesIndex();

    expect(
      await screen.findByText(/couldn't load configured instances: network down/i),
    ).toBeInTheDocument();
  });
});
