import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SetupPage } from "../pages/SetupPage";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

/**
 * Renders SetupPage at /setup with a stub "/" home route, so a successful
 * setup (which navigates to "/") is observable by the home marker appearing.
 */
function renderSetup() {
  return render(
    <MemoryRouter initialEntries={["/setup"]}>
      <Routes>
        <Route path="/setup" element={<SetupPage />} />
        <Route path="/" element={<div>Home view</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("SetupPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the first-run credential form", () => {
    renderSetup();
    expect(screen.getByRole("heading", { name: /create your account/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/^username$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
  });

  it("rejects mismatched passwords before calling the API", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderSetup();

    fireEvent.change(screen.getByLabelText(/^username$/i), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "secret-pass" } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "different" } });
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    expect(screen.getByText(/passwords don't match/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("creates the credential and navigates into the app on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ needs_setup: false, authenticated: true }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderSetup();

    fireEvent.change(screen.getByLabelText(/^username$/i), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "secret-pass" } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "secret-pass" } });
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText("Home view")).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/setup");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      username: "operator",
      password: "secret-pass",
    });
  });

  it("surfaces the API error when setup is rejected (e.g. already completed)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Setup has already been completed." }, 409)),
    );
    renderSetup();

    fireEvent.change(screen.getByLabelText(/^username$/i), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "secret-pass" } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "secret-pass" } });
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText(/setup has already been completed/i)).toBeInTheDocument();
  });
});
