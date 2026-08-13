import { render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { routes } from "../routes/router";

// See systemNav.test.tsx for why `Request` needs a jsdom-safe stub: rendering
// the real router's redirects/navigations builds an internal `Request` even
// with no loaders, and Node's built-in `Request` rejects jsdom's substitute
// AbortSignal.
vi.stubGlobal(
  "Request",
  class TestRequest {
    constructor(input: unknown, init: RequestInit = {}) {
      Object.assign(this, { url: String(input), ...init });
    }
  },
);

// FileDetailPage fetches from several endpoints on mount (the file itself,
// job history, settings); stub each with a minimal successful response so
// landing on it (post-redirect) doesn't hit the network or trigger act()
// warnings from an unmocked fetch. Response contents beyond the file's own
// shape are irrelevant here -- FileDetailPage's own behaviour is covered by
// fileDetailPage.test.tsx.
vi.stubGlobal(
  "fetch",
  vi.fn((url: string) => {
    if (url.startsWith("/api/files/")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            id: 42,
            file_path: "/media/movie.mkv",
            missing_targets: [],
            created_at: "2026-07-01T00:00:00Z",
            updated_at: "2026-07-01T00:00:00Z",
            library_node_id: null,
            node_type: null,
            tracked: null,
          }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
  }),
);

/**
 * COL-203: `/wanted/:fileId` (the pre-COL-203 per-file detail path) redirects
 * to `/files/:fileId`, preserving the id -- uses the real production `routes`
 * (from `routes/router.tsx`), same pattern as `systemNav.test.tsx`/
 * `settingsNav.test.tsx`, so this exercises the actual redirect the app
 * ships rather than a hand-rolled fixture.
 */
describe("WantedFileRedirect (COL-203)", () => {
  it("redirects /wanted/:fileId to /files/:fileId, preserving the id", async () => {
    const router = createMemoryRouter(routes, { initialEntries: ["/wanted/42"] });
    render(<RouterProvider router={router} />);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/files/42");
    });
    expect(await screen.findByText("/media/movie.mkv")).toBeInTheDocument();
  });
});
