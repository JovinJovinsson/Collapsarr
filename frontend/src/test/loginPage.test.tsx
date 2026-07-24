import { fireEvent, render, screen } from "@testing-library/react";
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
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/login");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
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

  it("validates that both fields are filled before calling the API", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    renderLogin();

    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(screen.getByText(/enter your username and password/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
