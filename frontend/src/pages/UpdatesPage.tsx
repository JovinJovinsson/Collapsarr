import { Download } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

import {
  dismissUpdateStatus,
  fetchUpdateStatus,
  recheckUpdateStatus,
  undismissUpdateStatus,
} from "../api/updates";
import type { UpdateCheckState } from "../types/updates";

/** The Docker Hub repository the release pipeline publishes to (`.github/workflows/release.yml`'s `IMAGE_NAME`). */
const DOCKER_IMAGE = "odxnsson/collapsarr";

/** GitHub Releases page, source of the native (PyInstaller) archives (`StatusPage`'s `SOURCE_URL` origin). */
const RELEASES_URL = "https://github.com/JovinJovinsson/Collapsarr/releases";

/** Formats an ISO timestamp in the viewer's local time, or an em dash when absent/unparseable. */
function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

/**
 * Derives the Docker tag to pull from the latest known release tag.
 *
 * The release pipeline (`.github/workflows/release.yml`) publishes Docker
 * tags *without* the `v` prefix GitHub release tags carry (e.g. release tag
 * `v1.2.3` -> Docker tag `1.2.3`), so this strips a leading `v` rather than
 * passing `latestVersion` through verbatim. Falls back to `latest` when no
 * release has been fetched yet (`latestVersion` is `null`).
 */
function dockerTagFor(latestVersion: string | null): string {
  if (!latestVersion) return "latest";
  return latestVersion.replace(/^v/, "");
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; state: UpdateCheckState };

/**
 * The System → Updates view (COL-87): the running instance's version against
 * the latest known GitHub Release for the configured channel, sourced from
 * `GET /api/system/updates` (`fetchUpdateStatus`, `collapsarr/update_check/
 * routes.py`). Mirrors `HealthChecksPage`'s load/error/ready shape and its
 * page-level "Recheck now" action (COL-83's `handleRecheck`), but for the
 * single Update Check state rather than a list of per-check rows.
 *
 * An "update available" is informational, not a failure state (see
 * `CONTEXT.md`'s Update Check entry) -- this page and the app-wide indicator
 * (`UpdateIndicator`) deliberately do not reuse `HealthBanner`'s
 * error/warning-oriented styling.
 *
 * Changelog rendering and install-method-specific upgrade instructions
 * (COL-90, three-way switch added in COL-219 for `"native"`): the changelog
 * (raw Markdown from the GitHub Release body, `UpdateCheckState.changelog`)
 * renders via `react-markdown` -- no `dangerouslySetInnerHTML`, and the
 * `rehype-raw` plugin is deliberately not enabled, so raw HTML embedded in a
 * changelog body is never rendered as HTML (that content is externally
 * sourced, from a GitHub Release body). The "How to update" block switches on
 * the API's `install_method` field (`"docker"` | `"pipx"` | `"native"`,
 * formerly the `is_docker` boolean, COL-215): `docker pull`/recreate for
 * `"docker"`, `pipx upgrade`/`pip install --upgrade` for `"pipx"`, and a
 * download-the-archive-and-replace-the-install-folder walkthrough for
 * `"native"` (PyInstaller build, COL-216+) -- the native branch also calls
 * out that the database/config are safe because they live in the OS
 * user-data directory (`platformdirs.user_data_dir("collapsarr")`,
 * `collapsarr/system/info.py`'s `data_dir`), not inside the install folder
 * being replaced. See `docs/adr/0001-update-check-detect-notify-only.md` for
 * why detection is backend-only and why no code path here executes any of
 * these commands itself -- purely informational, same as the docker/pipx
 * branches.
 *
 * COL-89 adds Dismiss/Undismiss actions (`POST /api/system/updates/dismiss` /
 * `.../undismiss`, `api/updates.ts`), mirroring `HealthChecksPage`'s per-row
 * dismiss/undismiss -- but scoped to the single Update Check row rather than
 * a list. "Dismiss" is only offered while an update is available and not
 * already dismissed; "Undismiss" only once it is. The server auto-clears a
 * dismissal the moment a newer release is published (COL-89's edge-triggered
 * reconciliation), so a stale dismissal never silently hides a genuinely new
 * update -- this page simply reflects whatever `dismissed_at` the server
 * reports on each load/action.
 */
export function UpdatesPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const [dismissing, setDismissing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchUpdateStatus()
      .then((update) => {
        if (!cancelled) {
          setState({ status: "ready", state: update });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleRecheck() {
    setRechecking(true);
    setActionError(null);
    try {
      const update = await recheckUpdateStatus();
      setState({ status: "ready", state: update });
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to check for updates.");
    } finally {
      setRechecking(false);
    }
  }

  async function handleDismiss() {
    setDismissing(true);
    setActionError(null);
    try {
      const update = await dismissUpdateStatus();
      setState({ status: "ready", state: update });
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to dismiss the update notice.");
    } finally {
      setDismissing(false);
    }
  }

  async function handleUndismiss() {
    setDismissing(true);
    setActionError(null);
    try {
      const update = await undismissUpdateStatus();
      setState({ status: "ready", state: update });
    } catch (error: unknown) {
      setActionError(
        error instanceof Error ? error.message : "Failed to undismiss the update notice."
      );
    } finally {
      setDismissing(false);
    }
  }

  return (
    <section className="view">
      <header className="view__header view__header--row">
        <div>
          <h1 className="view__title">Updates</h1>
          <p className="view__summary">
            Compares this install's running version against the latest release on your configured
            update channel.
          </p>
        </div>
        <div className="view__actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={handleRecheck}
            disabled={rechecking}
          >
            {rechecking ? "Checking…" : "Check now"}
          </button>
        </div>
      </header>

      {actionError && <p className="view__error">{actionError}</p>}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading update status…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <Download width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load update status: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && (
        <div className="panel update-panel">
          <dl className="update-panel__grid">
            <div className="update-panel__row">
              <dt>Running version</dt>
              <dd>{state.state.running_version}</dd>
            </div>
            <div className="update-panel__row">
              <dt>Latest version</dt>
              <dd>{state.state.latest_version ?? "—"}</dd>
            </div>
            <div className="update-panel__row">
              <dt>Last checked</dt>
              <dd>{formatTimestamp(state.state.checked_at)}</dd>
            </div>
          </dl>

          <div className="update-panel__status-row">
            <p
              className={`update-panel__status${
                state.state.update_available ? " update-panel__status--available" : ""
              }`}
            >
              <Download width={16} height={16} className="update-panel__status-icon" />
              {state.state.update_available
                ? `An update is available${state.state.latest_version ? ` (${state.state.latest_version})` : ""}.`
                : "You're up to date."}
              {state.state.dismissed_at && (
                <span className="update-panel__dismissed-badge">Dismissed</span>
              )}
            </p>
            {state.state.update_available && !state.state.dismissed_at && (
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={handleDismiss}
                disabled={dismissing}
              >
                {dismissing ? "Dismissing…" : "Dismiss"}
              </button>
            )}
            {state.state.dismissed_at && (
              <button
                type="button"
                className="btn btn--secondary btn--sm"
                onClick={handleUndismiss}
                disabled={dismissing}
              >
                {dismissing ? "Undismissing…" : "Undismiss"}
              </button>
            )}
          </div>

          {state.state.update_available && state.state.changelog && (
            <div className="update-panel__changelog">
              <h2 className="update-panel__changelog-title">
                {state.state.latest_version_label ?? state.state.latest_version}
              </h2>
              {/* No `dangerouslySetInnerHTML`, and `rehype-raw` is deliberately not
                  enabled -- `changelog` is externally-sourced (a GitHub Release
                  body), so raw HTML embedded in it must never be rendered as HTML. */}
              <div className="update-panel__changelog-body">
                <ReactMarkdown>{state.state.changelog}</ReactMarkdown>
              </div>
            </div>
          )}

          {state.state.update_available && (
            <div className="update-panel__instructions">
              <h2 className="update-panel__instructions-title">How to update</h2>
              {state.state.install_method === "docker" && (
                <ol className="update-panel__instructions-steps">
                  <li>
                    Pull the new image:
                    <pre className="update-panel__command">
                      <code>
                        docker pull {DOCKER_IMAGE}:{dockerTagFor(state.state.latest_version)}
                      </code>
                    </pre>
                  </li>
                  <li>
                    Recreate the container so it picks up the freshly pulled image:
                    <pre className="update-panel__command">
                      <code>docker compose up -d</code>
                    </pre>
                    (or, without Compose:{" "}
                    <code>docker stop collapsarr &amp;&amp; docker rm collapsarr</code>, then
                    re-run your <code>docker run</code> command.)
                  </li>
                </ol>
              )}
              {state.state.install_method === "pipx" && (
                <ol className="update-panel__instructions-steps">
                  <li>
                    Using pipx:
                    <pre className="update-panel__command">
                      <code>pipx upgrade collapsarr</code>
                    </pre>
                  </li>
                  <li>
                    Or, using pip directly:
                    <pre className="update-panel__command">
                      <code>pip install --upgrade collapsarr</code>
                    </pre>
                  </li>
                </ol>
              )}
              {state.state.install_method === "native" && (
                <>
                  <ol className="update-panel__instructions-steps">
                    <li>
                      Download the new archive for your platform from the{" "}
                      <a href={RELEASES_URL} target="_blank" rel="noreferrer">
                        release page
                      </a>
                      .
                    </li>
                    <li>Replace the install folder with the contents of the new archive.</li>
                    <li>Restart Collapsarr.</li>
                  </ol>
                  <p className="update-panel__instructions-note">
                    Your database and settings are safe: they&apos;re stored in your OS user-data
                    directory, not inside the install folder you&apos;re replacing.
                  </p>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
