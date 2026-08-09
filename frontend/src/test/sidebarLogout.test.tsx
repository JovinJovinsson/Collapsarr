import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "../components/Sidebar";
import { HealthProvider } from "../components/HealthProvider";
import { InstancesProvider } from "../components/InstancesProvider";

/**
 * Renders the Sidebar at "/" with a stub /login route so that clicking "Sign
 * out" (which POSTs /api/auth/logout then navigates to /login) is observable by
 * the login marker appearing. Wrapped in `InstancesProvider` (COL-100 code
 * review) since `Sidebar`'s `LibraryNavSection` reads the shared instance
 * list via `useInstances()`, which requires the provider even while
 * collapsed, and in `HealthProvider` (COL-124 code review) since `Sidebar`'s
 * version footer reads the shared `GET /health` state via `useHealth()`.
 */
function renderSidebar() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <HealthProvider>
        <InstancesProvider>
          <Routes>
            <Route path="/" element={<Sidebar />} />
            <Route path="/login" element={<div>Login view</div>} />
          </Routes>
        </InstancesProvider>
      </HealthProvider>
    </MemoryRouter>,
  );
}

describe("Sidebar sign-out (COL-50)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("clears the session and returns to /login", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) });
    vi.stubGlobal("fetch", fetchMock);
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /sign out/i }));

    expect(await screen.findByText("Login view")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/logout",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("Sidebar version display (COL-124)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("displays the app version from the shared health context", async () => {
    const fetchMock = vi.fn().mockImplementation((url) => {
      if (url === "/health") {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            status: "ok",
            version: "v1.2.3",
            warnings: [],
          }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSidebar();

    expect(await screen.findByText("v1.2.3")).toBeInTheDocument();
    // Exactly one /health fetch for the whole tree -- HealthProvider is the
    // single shared fetch, not one per consumer.
    expect(fetchMock.mock.calls.filter(([url]) => url === "/health")).toHaveLength(1);
  });

  it("renders nothing for the version while the health fetch is in flight", async () => {
    let resolveHealth: (value: unknown) => void = () => {};
    const fetchMock = vi.fn().mockImplementation((url) => {
      if (url === "/health") {
        return new Promise((resolve) => {
          resolveHealth = resolve;
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderSidebar();

    expect(screen.queryByText("Unknown")).not.toBeInTheDocument();
    expect(document.querySelector(".sidebar__version")).not.toBeInTheDocument();

    resolveHealth({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: "ok", version: "v1.2.3", warnings: [] }),
    });
    expect(await screen.findByText("v1.2.3")).toBeInTheDocument();
  });

  it("displays 'Unknown' when the health fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("Network error"));
    vi.stubGlobal("fetch", fetchMock);
    renderSidebar();

    await waitFor(() => expect(screen.getByText("Unknown")).toBeInTheDocument());
  });
});
