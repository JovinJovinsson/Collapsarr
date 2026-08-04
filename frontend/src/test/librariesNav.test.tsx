import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "../components/Sidebar";
import { InstancesProvider } from "../components/InstancesProvider";
import type { ArrInstance } from "../types/instances";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

function mockInstancesApi(instances: ArrInstance[]) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/instances") return jsonResponse(instances);
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
 */
function renderSidebar(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <InstancesProvider>
        <Sidebar />
      </InstancesProvider>
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
