import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UpdatesPage } from "../pages/UpdatesPage";
import type { UpdateCheckState } from "../types/updates";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

const upToDate: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.2.3",
  latest_version_label: "v1.2.3",
  changelog: null,
  checked_at: "2026-08-02T10:00:00Z",
  update_available: false,
  dismissed_at: null,
  is_docker: false,
};

const updateAvailable: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: "v1.3.0",
  latest_version_label: "Collapsarr v1.3.0",
  changelog: "- added things\n- fixed things",
  checked_at: "2026-08-02T10:00:00Z",
  update_available: true,
  dismissed_at: null,
  is_docker: false,
};

const updateAvailableDocker: UpdateCheckState = {
  ...updateAvailable,
  is_docker: true,
};

const updateDismissed: UpdateCheckState = {
  ...updateAvailable,
  dismissed_at: "2026-08-02T11:00:00Z",
};

const neverChecked: UpdateCheckState = {
  running_version: "1.2.3",
  latest_version: null,
  latest_version_label: null,
  changelog: null,
  checked_at: null,
  update_available: true,
  dismissed_at: null,
  is_docker: false,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UpdatesPage", () => {
  it("shows a loading state, then the up-to-date state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(upToDate)));
    render(<UpdatesPage />);

    expect(screen.getByText(/loading update status/i)).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());
    expect(screen.getByText("v1.2.3")).toBeInTheDocument();
    expect(screen.getByText(/you're up to date/i)).toBeInTheDocument();
  });

  it("shows the update-available state, including the changelog", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(updateAvailable)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    expect(screen.getByText(/an update is available/i)).toHaveTextContent("v1.3.0");
    expect(screen.getAllByText("v1.3.0").length).toBeGreaterThan(0);
    expect(screen.getByText("Collapsarr v1.3.0")).toBeInTheDocument();
    expect(screen.getByText(/added things/)).toBeInTheDocument();
  });

  // COL-90: changelog Markdown rendering.

  it("renders changelog Markdown (headings, bullet lists, links) as formatted content", async () => {
    const markdownChangelog: UpdateCheckState = {
      ...updateAvailable,
      changelog: "## Highlights\n\n- fixed a bug\n\nSee the [full notes](https://example.com/notes).",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(markdownChangelog)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());

    // A `##` heading becomes a real heading element, not literal "##" text.
    const heading = screen.getByRole("heading", { name: "Highlights" });
    expect(heading.tagName).toBe("H2");
    // A `- ` bullet becomes a real list item.
    const listItem = screen.getByText("fixed a bug");
    expect(listItem.tagName).toBe("LI");
    // A `[text](url)` link becomes a real anchor with the target href.
    const link = screen.getByRole("link", { name: "full notes" });
    expect(link).toHaveAttribute("href", "https://example.com/notes");
  });

  it("never renders raw HTML embedded in the changelog body as HTML", async () => {
    const htmlInjectionChangelog: UpdateCheckState = {
      ...updateAvailable,
      changelog: '<img src="x" onerror="window.__pwned = true" /><script>window.__pwned = true</script>',
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(htmlInjectionChangelog)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());

    // Neither tag was mounted as a real DOM element -- `rehype-raw` is
    // deliberately not enabled, so react-markdown treats the raw HTML as
    // inert text rather than rendering it (which would otherwise let an
    // externally-sourced changelog inject an `onerror` handler or a
    // `<script>` tag).
    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(document.querySelector("script")).not.toBeInTheDocument();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  // COL-90: install-method instructions.

  it("shows docker pull + recreate-container instructions when the API reports Docker", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(updateAvailableDocker)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/how to update/i)).toBeInTheDocument());
    expect(screen.getByText(/docker pull odxnsson\/collapsarr:1\.3\.0/)).toBeInTheDocument();
    expect(screen.getByText(/docker compose up -d/)).toBeInTheDocument();
    expect(screen.queryByText(/pipx upgrade collapsarr/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pip install --upgrade collapsarr/)).not.toBeInTheDocument();
  });

  it("shows pipx and pip instructions when the API does not report Docker", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(updateAvailable)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/how to update/i)).toBeInTheDocument());
    expect(screen.getByText(/pipx upgrade collapsarr/)).toBeInTheDocument();
    expect(screen.getByText(/pip install --upgrade collapsarr/)).toBeInTheDocument();
    expect(screen.queryByText(/docker pull/)).not.toBeInTheDocument();
  });

  it("does not show install instructions when there is no update available", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(upToDate)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
    expect(screen.queryByText(/how to update/i)).not.toBeInTheDocument();
  });

  it("handles a never-checked state without a latest version", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(neverChecked)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    // "Latest version" row falls back to an em dash when null.
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThan(0);
  });

  it("shows an error state when the fetch fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<UpdatesPage />);

    await waitFor(() =>
      expect(screen.getByText(/couldn.t load update status: network down/i)).toBeInTheDocument()
    );
  });

  it("shows a Check now button that refreshes the state without a full page reload", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(upToDate))
      .mockResolvedValueOnce(jsonResponse(updateAvailable));
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Check now" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/system/updates/recheck",
      expect.objectContaining({ method: "POST" })
    );
    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    // Exactly the recheck round-trip -- no extra GET afterwards, since the
    // recheck response already carries the full, fresh state.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows an error banner when a recheck fails, leaving the stale state in place", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(upToDate))
      .mockResolvedValueOnce(jsonResponse({ detail: "scheduler unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Check now" }));

    await waitFor(() => expect(screen.getByText(/scheduler unavailable/i)).toBeInTheDocument());
    expect(screen.getByText(/you're up to date/i)).toBeInTheDocument();
  });

  // COL-89: dismiss/undismiss.

  it("offers a Dismiss button when an update is available and not yet dismissed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(updateAvailable)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/an update is available/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undismiss" })).not.toBeInTheDocument();
    expect(screen.queryByText(/dismissed/i)).not.toBeInTheDocument();
  });

  it("does not offer Dismiss when there is no update available", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(upToDate)));
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByText(/you're up to date/i)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });

  it("dismissing calls the dismiss endpoint and switches to an Undismiss button", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(updateAvailable))
      .mockResolvedValueOnce(jsonResponse(updateDismissed));
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/system/updates/dismiss",
      expect.objectContaining({ method: "POST" })
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Undismiss" })).toBeInTheDocument()
    );
    expect(screen.getByText(/dismissed/i)).toBeInTheDocument();
  });

  it("undismissing calls the undismiss endpoint and switches back to a Dismiss button", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(updateDismissed))
      .mockResolvedValueOnce(jsonResponse(updateAvailable));
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdatesPage />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Undismiss" })).toBeInTheDocument()
    );
    fireEvent.click(screen.getByRole("button", { name: "Undismiss" }));

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/system/updates/undismiss",
      expect.objectContaining({ method: "POST" })
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument());
    expect(screen.queryByText(/dismissed/i)).not.toBeInTheDocument();
  });

  it("shows an error when a dismiss action fails, leaving the button in place", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(updateAvailable))
      .mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdatesPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(screen.getByText(/boom/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });
});
