import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectSection } from "../components/settings/ConnectSection";
import type { NotifierConfig } from "../types/notifiers";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const baseConfig: NotifierConfig = {
  webhook_url: "https://example.com/hook",
  webhook_enabled: true,
  discord_webhook_url: null,
  discord_enabled: false,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
};

describe("ConnectSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the current notifier config from a mocked GET", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(baseConfig)));
    render(<ConnectSection />);

    expect(await screen.findByLabelText(/^webhook url$/i)).toHaveValue("https://example.com/hook");
    expect(screen.getByRole("checkbox", { name: /enable generic webhook notifications/i })).toBeChecked();
    expect(screen.getByLabelText(/discord webhook url/i)).toHaveValue("");
    expect(screen.getByRole("checkbox", { name: /enable discord notifications/i })).not.toBeChecked();
  });

  it("toggles Discord on, sets its URL, and saves via PUT", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        const body = JSON.parse(String(init?.body));
        return Promise.resolve(
          jsonResponse({
            ...baseConfig,
            discord_webhook_url: body.discord_webhook_url,
            discord_enabled: body.discord_enabled,
          }),
        );
      }
      return Promise.resolve(jsonResponse(baseConfig));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConnectSection />);
    await screen.findByLabelText(/^webhook url$/i);

    fireEvent.change(screen.getByLabelText(/discord webhook url/i), {
      target: { value: "https://discord.com/api/webhooks/123/abc" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /enable discord notifications/i }));
    fireEvent.click(screen.getByRole("button", { name: /save connect settings/i }));

    expect(await screen.findByText(/saved\./i)).toBeInTheDocument();

    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(putCall).toBeDefined();
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.discord_webhook_url).toBe("https://discord.com/api/webhooks/123/abc");
    expect(putBody.discord_enabled).toBe(true);
    // The webhook fields are re-sent unchanged alongside the Discord edit.
    expect(putBody.webhook_url).toBe("https://example.com/hook");
    expect(putBody.webhook_enabled).toBe(true);
  });

  it("sends null for a blank URL (clears the stored value)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(baseConfig));
    vi.stubGlobal("fetch", fetchMock);

    render(<ConnectSection />);
    const webhookUrlInput = await screen.findByLabelText(/^webhook url$/i);
    fireEvent.change(webhookUrlInput, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save connect settings/i }));

    await screen.findByText(/saved\./i);
    const putCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const putBody = JSON.parse(String((putCall?.[1] as RequestInit).body));
    expect(putBody.webhook_url).toBeNull();
  });

  it("surfaces an API error from a failed save", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "PUT") {
        return Promise.resolve(jsonResponse({ detail: "invalid webhook url" }, 400));
      }
      return Promise.resolve(jsonResponse(baseConfig));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ConnectSection />);
    await screen.findByLabelText(/^webhook url$/i);
    fireEvent.click(screen.getByRole("button", { name: /save connect settings/i }));

    expect(await screen.findByText("invalid webhook url")).toBeInTheDocument();
  });

  it("renders an error state when the initial load fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<ConnectSection />);

    expect(await screen.findByText(/couldn't load connect settings: network down/i)).toBeInTheDocument();
  });
});
