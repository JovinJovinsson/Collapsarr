import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { setupCredential } from "../api/auth";
import { BrandMark } from "../components/icons";

/**
 * First-run setup (COL-50): the one-time screen that creates the single
 * operator credential. The server's enforcement middleware redirects every UI
 * route here until a credential exists, so this is the first thing a fresh
 * install shows. On success the server logs the new operator straight in (a
 * session cookie is set on the setup response), so we navigate into the app.
 *
 * The credential is Radarr-style -- a single username/password, no multi-user.
 * `setupCredential` fails with a 409 if a credential already exists, which
 * surfaces here as an error (the gate can only be closed once).
 */
export function SetupPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!username.trim()) {
      setError("Choose a username.");
      return;
    }
    if (!password) {
      setError("Choose a password.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await setupCredential(username.trim(), password);
      // The setup response set a session cookie: the gate is closed and we're
      // logged in, so drop the user straight into the app.
      navigate("/", { replace: true });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Setup failed.");
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

        <h1 className="auth-card__title">Create your account</h1>
        <p className="auth-card__subtitle">
          Set the username and password you&apos;ll use to sign in. This is a one-time step.
        </p>

        <div className="form-field">
          <label htmlFor="setup-username">Username</label>
          <input
            id="setup-username"
            name="username"
            type="text"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </div>

        <div className="form-field">
          <label htmlFor="setup-password">Password</label>
          <input
            id="setup-password"
            name="password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>

        <div className="form-field">
          <label htmlFor="setup-confirm">Confirm password</label>
          <input
            id="setup-confirm"
            name="confirm"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
        </div>

        {error && <p className="form-error">{error}</p>}

        <button type="submit" className="btn btn--primary auth-card__submit" disabled={submitting}>
          {submitting ? "Creating account…" : "Create account"}
        </button>
      </form>
    </main>
  );
}
