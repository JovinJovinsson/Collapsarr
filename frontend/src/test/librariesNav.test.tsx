import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "../components/Sidebar";
import { HealthProvider } from "../components/HealthProvider";
import { InstancesProvider } from "../components/InstancesProvider";
import type { ArrInstance } from "../types/instances";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

/**
 * `Sidebar` also reads the shared `GET /health` state via `useHealth()`
 * (COL-124 code review), so every render needs a `/health` response too --
 * the version footer is incidental to these tests, so an "ok" stub with no
 * warnings keeps them focused on the Libraries nav behaviour under test.
 */
function mockInstancesApi(instances: ArrInstance[]) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/instances") return jsonResponse(instances);
    if (url === "/health") return jsonResponse({ status: "ok", version: "0.1.0", warnings: [] });
    throw new Error(`Unhandled request in test mock: GET ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
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

/**
 * `Sidebar` needs Router context (`LibraryNavSection` reads `useLocation`)
 * plus `InstancesProvider` (COL-100 code review): `LibraryNavSection` reads
 * the shared instance list via `useInstances()` rather than fetching its own.
 * Also needs `HealthProvider` (COL-124 code review): `Sidebar`'s version
 * footer reads the shared `GET /health` state via `useHealth()`.
 */
function renderSidebar(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <HealthProvider>
        <InstancesProvider>
          <Sidebar />
        </InstancesProvider>
      </HealthProvider>
    </MemoryRouter>,
  );
}

describe("Libraries sidebar nav (COL-100)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders a 'Libraries' primary nav entry", async () => {
    mockInstancesApi([sonarr, radarr]);
    renderSidebar("/wanted");

    expect(await screen.findByRole("link", { name: /libraries/i })).toBeInTheDocument();
  });

  it("is collapsed (no per-instance sub-items) while a different section is active", async () => {
    mockInstancesApi([sonarr, radarr]);
    renderSidebar("/wanted");

    await screen.findByRole("link", { name: /libraries/i });
    expect(screen.queryByText("Sonarr Prod")).not.toBeInTheDocument();
    expect(screen.queryByText("Radarr Prod")).not.toBeInTheDocument();
  });

  it("expands to one sub-item per configured instance when Libraries is selected", async () => {
    mockInstancesApi([sonarr, radarr]);
    renderSidebar("/wanted");

    fireEvent.click(screen.getByRole("link", { name: /libraries/i }));

    expect(await screen.findByText("Sonarr Prod")).toBeInTheDocument();
    expect(screen.getByText("Radarr Prod")).toBeInTheDocument();
  });

  it("stays expanded and highlights the active instance on an instance's own page", async () => {
    mockInstancesApi([sonarr, radarr]);
    renderSidebar("/libraries/1");

    const sonarrLink = await screen.findByRole("link", { name: "Sonarr Prod" });
    const radarrLink = screen.getByRole("link", { name: "Radarr Prod" });
    expect(sonarrLink.className).toContain("sidebar__sublink--active");
    expect(radarrLink.className).not.toContain("sidebar__sublink--active");
  });

  it("selecting a sub-item keeps the section expanded and marks it active", async () => {
    mockInstancesApi([sonarr, radarr]);
    renderSidebar("/wanted");

    fireEvent.click(screen.getByRole("link", { name: /libraries/i }));
    const radarrLink = await screen.findByRole("link", { name: "Radarr Prod" });
    fireEvent.click(radarrLink);

    expect(await screen.findByRole("link", { name: "Sonarr Prod" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Radarr Prod" }).className).toContain(
      "sidebar__sublink--active",
    );
  });

  it("shows a message instead of sub-items when no instances are configured", async () => {
    mockInstancesApi([]);
    renderSidebar("/libraries");

    expect(await screen.findByText(/no instances configured/i)).toBeInTheDocument();
  });
});
