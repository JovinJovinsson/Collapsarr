/**
 * Client for the Forms auth flow (`/api/auth/*`, COL-50).
 *
 * These endpoints gate the whole UI: first-run `setup` creates the single
 * operator credential, `login` opens a signed-cookie session, `logout` clears
 * it, and `status` reports whether setup is still needed / whether this browser
 * is logged in. The session is a cookie the browser sends automatically, so --
 * unlike the `/api` data endpoints -- these don't depend on the stored API key;
 * they still route through `apiFetch` only to reuse its error parsing.
 */

import type { AuthMethod } from "../types/settings";
import { apiErrorMessage, apiFetch } from "./client";

/** Mirrors `collapsarr.auth.routes.AuthStatus`. */
export interface AuthStatus {
  needs_setup: boolean;
  authenticated: boolean;
  /** Which method (COL-52) is active -- the Login page uses this to hide
   * "remember me" under Basic, a Forms-only concept. */
  auth_method: AuthMethod;
}

/** Reads first-run / session state (`GET /api/auth/status`). */
export async function fetchAuthStatus(): Promise<AuthStatus> {
  const response = await apiFetch("/api/auth/status");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load auth status (${response.status})`));
  }
  return (await response.json()) as AuthStatus;
}

/**
 * Creates the single credential on first run and logs in
 * (`POST /api/auth/setup`). Fails once setup has already been completed.
 */
export async function setupCredential(username: string, password: string): Promise<AuthStatus> {
  const response = await apiFetch("/api/auth/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Setup failed (${response.status})`));
  }
  return (await response.json()) as AuthStatus;
}

/**
 * Authenticates the credential and opens a session (`POST /api/auth/login`).
 * `remember` selects a long-lived cookie over a browser-session one.
 */
export async function login(
  username: string,
  password: string,
  remember: boolean,
): Promise<AuthStatus> {
  const response = await apiFetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, remember }),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Login failed (${response.status})`));
  }
  return (await response.json()) as AuthStatus;
}

/** Clears the session (`POST /api/auth/logout`). */
export async function logout(): Promise<void> {
  const response = await apiFetch("/api/auth/logout", { method: "POST" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Logout failed (${response.status})`));
  }
}
