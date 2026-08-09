import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "../components/Sidebar";
import { InstancesProvider } from "../components/InstancesProvider";

/**
 * Renders the Sidebar at "/" with a stub /login route so that clicking "Sign
 * out" (which POSTs /api/auth/logout then navigates to /login) is observable by
 * the login marker appearing. Wrapped in `InstancesProvider` (COL-100 code
 * review) since `Sidebar`'s `LibraryNavSection` reads the shared instance
 * list via `useInstances()`, which requires the provider even while collapsed.
 */
function renderSidebar() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <InstancesProvider>
        <Routes>
          <Route path="/" element={<Sidebar />} />
          <Route path="/login" element={<div>Login view</div>} />
        </Routes>
      </InstancesProvider>
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

  it("displays the app version fetched from GET /health", async () => {
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
  });

  it("displays 'Unknown' when the health fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("Network error"));
    vi.stubGlobal("fetch", fetchMock);
    renderSidebar();

    // The "Unknown" text should appear (displayed when version is null)
    expect(await screen.findByText("Unknown")).toBeInTheDocument();
  });
});
