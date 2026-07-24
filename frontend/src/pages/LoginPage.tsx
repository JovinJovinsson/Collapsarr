import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { fetchAuthStatus, login } from "../api/auth";
import { BrandMark } from "../components/icons";

/**
 * Forms login (COL-50): authenticates the single operator credential and opens
 * a signed-cookie session. The server redirects UI routes here once a
 * credential exists but no session is present. "Remember me" selects a
 * long-lived cookie over a browser-session one; on success we navigate into
 * the app, where the session cookie authenticates `/api` requests.
 *
 * Under the Basic auth method (COL-52) this page is normally unreachable --
 * the enforcement middleware challenges with `WWW-Authenticate: Basic`
 * instead of redirecting here -- but if it is ever reached directly (e.g. a
 * stale bookmark before a method switch takes effect in this browser),
 * "remember me" is hidden: it's a Forms-only concept, since Basic re-sends
 * credentials on every request rather than persisting a session choice.
 */
export function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [authMethod, setAuthMethod] = useState<"forms" | "basic">("forms");

  useEffect(() => {
    fetchAuthStatus()
      .then((status) => setAuthMethod(status.auth_method))
      .catch(() => {
        // Best-effort: keep the default ("forms", remember-me shown) if the
        // status fetch fails -- the login form itself still works either way.
      });
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError("Enter your username and password.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await login(username.trim(), password, remember);
      navigate("/", { replace: true });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Login failed.");
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-screen">
      <form className="auth-card" onSubmit={handleSubmit}>
        <div className="auth-card__brand">
          <span className="auth-card__brand-mark" aria-hidden>
            <BrandMark />
          </span>
          <span className="auth-card__brand-name">Collapsarr</span>
        </div>

        <h1 className="auth-card__title">Sign in</h1>
        <p className="auth-card__subtitle">Enter your credentials to continue.</p>

        <div className="form-field">
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            name="username"
            type="text"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </div>

        <div className="form-field">
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>

        {authMethod !== "basic" && (
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            Remember me on this device
          </label>
        )}

        {error && <p className="form-error">{error}</p>}

        <button type="submit" className="btn btn--primary auth-card__submit" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
