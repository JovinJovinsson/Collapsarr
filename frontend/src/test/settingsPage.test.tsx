import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsPage } from "../pages/SettingsPage";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

/**
 * Smoke test: renders the composed Settings view (COL-33) against a mocked
 * API and checks its one remaining section (Instances) mounts and shows its
 * empty state -- the deeper per-section behaviour (CRUD, validation, error
 * surfacing) is covered by `settingsInstances.test.tsx`. General moved to
 * its own page (COL-142), covered by `settingsGeneral.test.tsx`; Targets and
 * Connect moved to their own pages (COL-143), covered by
 * `settingsTargets.test.tsx`/`settingsConnect.test.tsx`; this smoke test no
 * longer asserts on any of them.
 */
describe("SettingsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("mounts the Instances section against a mocked API response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url === "/api/instances") return Promise.resolve(jsonResponse([]));
        return Promise.reject(new Error(`Unhandled request in test mock: ${url}`));
      }),
    );

    render(
      <MemoryRouter>
        <SettingsPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(await screen.findByText(/no arr instances configured yet/i)).toBeInTheDocument();
  });
});
