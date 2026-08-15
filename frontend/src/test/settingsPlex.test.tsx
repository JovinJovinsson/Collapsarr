import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PlexSection } from "../components/settings/PlexSection";
import type { PlexConnection } from "../types/plex";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) };
}

const unconfigured: PlexConnection = {
  base_url: "",
  has_token: false,
  status: "unknown",
  status_error: null,
  status_checked_at: null,
  version: null,
  created_at: "2026-08-13T00:00:00Z",
  updated_at: "2026-08-13T00:00:00Z",
};

const configured: PlexConnection = {
  base_url: "http://plex.local:32400",
  has_token: true,
  status: "ok",
  status_error: null,
  status_checked_at: "2026-08-13T00:05:00Z",
  version: "1.32.5.7349-8f4248874",
  created_at: "2026-08-13T00:00:00Z",
  updated_at: "2026-08-13T00:05:00Z",
};

describe("PlexSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the current connection from a mocked GET, with the token field blank", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(configured)));
    render(<PlexSection />);

    expect(await screen.findByLabelText(/base url/i)).toHaveValue("http://plex.local:32400");
    expect(screen.getByLabelText(/plex token/i)).toHaveValue("");
    expect(screen.getByText(/connected/i)).toBeInTheDocument();
    expect(screen.getByText(/1\.32\.5\.7349-8f4248874/)).toBeInTheDocument();
  });

  it("never renders the token anywhere on the page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(configured)));
    const { container } = render(<PlexSection />);
    await screen.findByLabelText(/base url/i);

    expect(container.innerHTML).not.toMatch(/plex-secret-token/i);
  });

  it("saves base URL + token via PUT and only sends token when typed", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(
          jsonResponse({
            ...configured,
            base_url: body.base_url,
            has_token: "token" in body ? true : configured.has_token,
          }),
        );
      }
      return Promise.resolve(jsonResponse(unconfigured));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<PlexSection />);
    await screen.findByLabelText(/base url/i);

    fireEvent.change(screen.getByLabelText(/base url/i), {
      target: { value: "http://plex.local:32400" },
    });
    fireEvent.change(screen.getByLabelText(/plex token/i), {
      target: { value: "plex-secret-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save plex connection/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();

    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(putCall).toBeDefined();
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.base_url).toBe("http://plex.local:32400");
    expect(putBody.token).toBe("plex-secret-token");
  });

  it("omits token from the PUT body when the field is left blank on an already-configured connection", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(jsonResponse({ ...configured, base_url: body.base_url }));
      }
      return Promise.resolve(jsonResponse(configured));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<PlexSection />);
    await screen.findByLabelText(/base url/i);

    fireEvent.change(screen.getByLabelText(/base url/i), {
      target: { value: "http://plex.local:32401" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save plex connection/i }));

    await screen.findByText(/saved\./i);
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.base_url).toBe("http://plex.local:32401");
    expect("token" in putBody).toBe(false);
  });

  it("requires a token before the connection has ever been configured", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(unconfigured)));
    render(<PlexSection />);
    await screen.findByLabelText(/base url/i);

    fireEvent.change(screen.getByLabelText(/base url/i), {
      target: { value: "http://plex.local:32400" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save plex connection/i }));

    expect(await screen.findByText(/plex token is required/i)).toBeInTheDocument();
  });

  it("surfaces an API error from a failed save", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse({ detail: "invalid base url" }, 400));
      }
      return Promise.resolve(jsonResponse(configured));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<PlexSection />);
    await screen.findByLabelText(/base url/i);
    fireEvent.click(screen.getByRole("button", { name: /save plex connection/i }));

    expect(await screen.findByText("invalid base url")).toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<PlexSection />);

    expect(await screen.findByText(/couldn't load plex connection: network down/i)).toBeInTheDocument();
  });
});
