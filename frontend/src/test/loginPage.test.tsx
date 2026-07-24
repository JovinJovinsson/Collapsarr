import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LoginPage } from "../pages/LoginPage";

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: () => Promise.resolve(body) };
}

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<div>Home view</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("LoginPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the login form with a remember-me option", () => {
    renderLogin();
    expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/^username$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /remember me/i })).not.toBeChecked();
  });

  it("logs in and navigates into the app, passing the remember flag", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ needs_setup: false, authenticated: true }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderLogin();

    fireEvent.change(screen.getByLabelText(/^username$/i), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "secret-pass" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /remember me/i }));
    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(await screen.findByText("Home view")).toBeInTheDocument();
    // The mount effect also fetches /api/auth/status (to learn the active
    // auth method), so find the login call specifically rather than
    // assuming it is the first one.
    const loginCall = fetchMock.mock.calls.find(([callUrl]) => callUrl === "/api/auth/login");
    expect(loginCall).toBeDefined();
    const [, init] = loginCall as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      username: "operator",
      password: "secret-pass",
      remember: true,
    });
  });

  it("rejects wrong credentials with the server's message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Invalid username or password." }, 401)),
    );
    renderLogin();

    fireEvent.change(screen.getByLabelText(/^username$/i), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(await screen.findByText(/invalid username or password/i)).toBeInTheDocument();
  });

  it("validates that both fields are filled before calling the login API", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ needs_setup: false, authenticated: false, auth_method: "forms" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderLogin();
    // Let the mount effect's /api/auth/status fetch settle before asserting,
    // so the state update it triggers doesn't leak into the next test.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(screen.getByText(/enter your username and password/i)).toBeInTheDocument();
    // The mount effect's /api/auth/status fetch is expected; only the login
    // submission itself must not have fired.
    expect(fetchMock.mock.calls.some(([callUrl]) => callUrl === "/api/auth/login")).toBe(false);
  });

  it("hides the remember-me option once the server reports the Basic auth method (COL-52)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ needs_setup: false, authenticated: false, auth_method: "basic" }),
      ),
    );
    renderLogin();

    expect(screen.getByLabelText(/^username$/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("checkbox", { name: /remember me/i })).not.toBeInTheDocument(),
    );
  });
});
