import { useEffect, useState } from "react";

import {
  dismissUpdateStatus,
  fetchUpdateStatus,
  recheckUpdateStatus,
  undismissUpdateStatus,
} from "../api/updates";
import { UpdateIcon } from "../components/icons";
import type { UpdateCheckState } from "../types/updates";

/** Formats an ISO timestamp in the viewer's local time, or an em dash when absent/unparseable. */
function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
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
 * Changelog rendering (Markdown) and install-method-specific upgrade
 * instructions are out of scope for this slice -- the changelog renders as
 * plain text; a later slice replaces it with rendered Markdown.
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
            <UpdateIcon width={28} height={28} />
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
              <UpdateIcon width={16} height={16} className="update-panel__status-icon" />
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
              <pre className="update-panel__changelog-body">{state.state.changelog}</pre>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
