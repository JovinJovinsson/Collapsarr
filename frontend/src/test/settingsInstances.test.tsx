import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InstancesSection } from "../components/settings/InstancesSection";
import type { ArrInstance, PathMapping } from "../types/instances";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

/** In-memory fake of the `/api/instances` + nested path-mappings REST surface (COL-27). */
function mockInstancesApi(initialInstances: ArrInstance[] = []) {
  let instances = [...initialInstances];
  const mappings: Record<number, PathMapping[]> = {};
  let nextInstanceId = 100;
  let nextMappingId = 1000;

  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;

    if (url === "/api/instances" && method === "GET") return jsonResponse(instances);

    if (url === "/api/instances" && method === "POST") {
      const created: ArrInstance = {
        id: nextInstanceId++,
        name: body.name,
        type: body.type,
        base_url: body.base_url,
        api_key: body.api_key,
        status: "unknown",
        status_error: null,
        status_checked_at: null,
        version: null,
        created_at: "2026-07-20T00:00:00Z",
        updated_at: "2026-07-20T00:00:00Z",
      };
      instances = [...instances, created];
      return jsonResponse(created, 201);
    }

    const instanceMatch = /^\/api\/instances\/(\d+)$/.exec(url);
    if (instanceMatch && method === "PUT") {
      const id = Number(instanceMatch[1]);
      instances = instances.map((instance) => (instance.id === id ? { ...instance, ...body } : instance));
      return jsonResponse(instances.find((instance) => instance.id === id));
    }
    if (instanceMatch && method === "DELETE") {
      const id = Number(instanceMatch[1]);
      instances = instances.filter((instance) => instance.id !== id);
      return jsonResponse(null, 204);
    }

    const mappingsListMatch = /^\/api\/instances\/(\d+)\/path-mappings$/.exec(url);
    if (mappingsListMatch && method === "GET") {
      const id = Number(mappingsListMatch[1]);
      return jsonResponse(mappings[id] ?? []);
    }
    if (mappingsListMatch && method === "POST") {
      const id = Number(mappingsListMatch[1]);
      const created: PathMapping = {
        id: nextMappingId++,
        instance_id: id,
        remote_prefix: body.remote_prefix,
        local_prefix: body.local_prefix,
        order: body.order ?? 0,
        created_at: "2026-07-20T00:00:00Z",
        updated_at: "2026-07-20T00:00:00Z",
      };
      mappings[id] = [...(mappings[id] ?? []), created];
      return jsonResponse(created, 201);
    }

    const mappingItemMatch = /^\/api\/instances\/(\d+)\/path-mappings\/(\d+)$/.exec(url);
    if (mappingItemMatch && method === "DELETE") {
      const instanceId = Number(mappingItemMatch[1]);
      const mappingId = Number(mappingItemMatch[2]);
      mappings[instanceId] = (mappings[instanceId] ?? []).filter((mapping) => mapping.id !== mappingId);
      return jsonResponse(null, 204);
    }

    throw new Error(`Unhandled request in test mock: ${method} ${url}`);
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
  status_checked_at: "2026-07-19T00:00:00Z",
  version: "4.0.0",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

describe("InstancesSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists instances from a mocked API response", async () => {
    mockInstancesApi([sonarr]);
    render(<InstancesSection />);

    expect(await screen.findByText("Sonarr Prod")).toBeInTheDocument();
    expect(screen.getByText("http://localhost:8989")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
  });

  it("renders an empty state with no instances configured", async () => {
    mockInstancesApi([]);
    render(<InstancesSection />);

    expect(await screen.findByText(/no arr instances configured yet/i)).toBeInTheDocument();
  });

  it("shows a validation error and does not submit when required fields are blank", async () => {
    const fetchMock = mockInstancesApi([]);
    render(<InstancesSection />);

    await screen.findByText(/no arr instances configured yet/i);
    fireEvent.click(screen.getByRole("button", { name: /add instance/i }));
    fireEvent.click(screen.getByRole("button", { name: /save instance/i }));

    expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith("/api/instances", expect.objectContaining({ method: "POST" }));
  });

  it("creates a new instance via the form", async () => {
    mockInstancesApi([]);
    render(<InstancesSection />);

    await screen.findByText(/no arr instances configured yet/i);
    fireEvent.click(screen.getByRole("button", { name: /add instance/i }));

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Radarr Prod" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "radarr" } });
    fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "http://localhost:7878" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "radarr-key" } });
    fireEvent.click(screen.getByRole("button", { name: /save instance/i }));

    expect(await screen.findByText("Radarr Prod")).toBeInTheDocument();
    expect(screen.getByText("http://localhost:7878")).toBeInTheDocument();
  });

  it("edits an existing instance", async () => {
    mockInstancesApi([sonarr]);
    render(<InstancesSection />);

    const row = (await screen.findByText("Sonarr Prod")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /^edit$/i }));

    const nameInput = screen.getByLabelText("Edit instance name");
    fireEvent.change(nameInput, { target: { value: "Sonarr Main" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(await screen.findByText("Sonarr Main")).toBeInTheDocument();
  });

  it("deletes an instance after confirmation", async () => {
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    mockInstancesApi([sonarr]);
    render(<InstancesSection />);

    const row = (await screen.findByText("Sonarr Prod")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /delete/i }));

    expect(await screen.findByText(/no arr instances configured yet/i)).toBeInTheDocument();
  });

  it("does not delete when the confirmation is dismissed", async () => {
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(false));
    mockInstancesApi([sonarr]);
    render(<InstancesSection />);

    const row = (await screen.findByText("Sonarr Prod")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /delete/i }));

    expect(await screen.findByText("Sonarr Prod")).toBeInTheDocument();
  });

  it("manages path mappings: adds one and shows it in the list", async () => {
    mockInstancesApi([sonarr]);
    render(<InstancesSection />);

    await screen.findByText("Sonarr Prod");
    fireEvent.click(screen.getByRole("button", { name: /manage mappings/i }));

    expect(await screen.findByText(/no path mappings configured/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /add mapping/i }));
    fireEvent.change(screen.getByLabelText(/remote prefix/i), { target: { value: "/tv" } });
    fireEvent.change(screen.getByLabelText(/local prefix/i), { target: { value: "/mnt/tv" } });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));

    expect(await screen.findByText("/tv")).toBeInTheDocument();
    expect(screen.getByText("/mnt/tv")).toBeInTheDocument();
  });

  it("surfaces an API error from a failed create", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValueOnce(jsonResponse([]));

    render(<InstancesSection />);
    await screen.findByText(/no arr instances configured yet/i);

    fireEvent.click(screen.getByRole("button", { name: /add instance/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Radarr" } });
    fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "http://localhost:7878" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "key" } });
    fireEvent.click(screen.getByRole("button", { name: /save instance/i }));

    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});
