import { Download } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

import { fetchJobQueue } from "../api/activity";
import { fetchSettings } from "../api/settings";
import {
  applySelfUpdate,
  dismissUpdateStatus,
  fetchUpdateStatus,
  recheckUpdateStatus,
  undismissUpdateStatus,
} from "../api/updates";
import { SelfUpdateModal } from "../components/SelfUpdateModal";
import { UpdateInstructions } from "../components/UpdateInstructions";
import type { UpdateChannel } from "../types/settings";
import type { SelfUpdateFlow, UpdateCheckState } from "../types/updates";

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
 * Load state for the persisted `update_channel` setting (COL-228) --
 * `canOfferSelfUpdate` below needs it: Self-Update ships **stable-channel-
 * only** (`docs/adr/0009-self-update-staged-handoff-with-auto-rollback.md`),
 * a narrower gate than {@link UpdateCheckState.update_available}, which
 * reflects whatever channel is *configured* (possibly `beta`). Fetched
 * independently of the Update Check state above -- same one-shot-on-mount
 * shape `QueuePage` uses for its own `fetchSettings()` call.
 */
type SettingsLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; updateChannel: UpdateChannel };

/**
 * The confirmation modal's state (COL-228) -- `null` while closed.
 * `runningJobCount` is resolved from `GET /api/jobs/queue` *before* the
 * modal opens (see `handleUpdateNowClick`), so `SelfUpdateModal` always
 * already knows which of its two shapes to render, rather than the modal
 * itself re-deriving it.
 */
type SelfUpdateModalState = { runningJobCount: number } | null;

/**
 * Page-level notice after an "Update Now" action settles (COL-228) --
 * mirrors `QueuePage`'s `ActionNotice`/`ClearQueueNotice` named-notice-type
 * convention rather than an inline object-literal `useState` type.
 * `"error"` covers a failure to even resolve the running-Job count (the
 * modal never opened) or an apply-endpoint failure surfaced after the modal
 * already closed (there isn't one today -- a responded apply failure stays
 * inside the still-open modal via `applyError` instead, see
 * `handleConfirmSelfUpdate` -- but the type stays two-toned for symmetry
 * with every other notice in this codebase). `"success"` is the one case
 * that actually fires today: the apply call was accepted.
 */
type UpdateNowNotice = { tone: "success" | "error"; text: string } | null;

/**
 * Whether Self-Update (COL-228, `CONTEXT.md`'s **Self-Update** entry) can
 * offer an "Update Now" action for the current Update Check state --
 * `native`/`pipx` only (`docker` remains manual-only, an image cannot
 * self-replace), and only once a **stable**-channel update is available:
 * `update.update_available` alone isn't sufficient, since it reflects
 * whatever channel is *configured* (`settings.updateChannel`) -- a `beta`-
 * channel "update available" is never a valid Self-Update target
 * (`collapsarr/self_update/apply.py::stable_update_target`), so this page
 * falls back to {@link UpdateInstructions} in that case exactly as it would
 * for a `docker` install with nothing else it can offer.
 */
function canOfferSelfUpdate(update: UpdateCheckState, settings: SettingsLoadState): boolean {
  if (!update.update_available) return false;
  if (update.install_method !== "native" && update.install_method !== "pipx") return false;
  return settings.status === "ready" && settings.updateChannel === "stable";
}

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
 * Changelog rendering (COL-90): the changelog (raw Markdown from the GitHub
 * Release body, `UpdateCheckState.changelog`) renders via `react-markdown`
 * -- no `dangerouslySetInnerHTML`, and the `rehype-raw` plugin is
 * deliberately not enabled, so raw HTML embedded in a changelog body is
 * never rendered as HTML (that content is externally sourced, from a GitHub
 * Release body).
 *
 * The "How to update" block lives in its own `UpdateInstructions`
 * subcomponent (`components/UpdateInstructions.tsx`, COL-91/COL-228), shown
 * whenever Self-Update itself has nothing to offer for the current state.
 *
 * COL-228 adds the **Self-Update** "Update Now" action (`CONTEXT.md`'s
 * **Self-Update** entry, `docs/adr/0009-self-update-staged-handoff-with-
 * auto-rollback.md`): shown instead of `UpdateInstructions` -- see
 * {@link canOfferSelfUpdate} -- only for a `native`/`pipx` install with a
 * **stable**-channel update available (Self-Update ships stable-only; a
 * `docker` install, or a `beta`-channel "update available", falls back to
 * the static instructions exactly as before). Clicking it resolves the
 * currently-`running` Job count (`GET /api/jobs/queue`, `handleUpdateNowClick`)
 * *before* opening `SelfUpdateModal`, so the modal already knows whether to
 * show the plain confirm, the job-count-aware Cancel & Restart Now / Wait &
 * Restart choice (`pipx`), or -- for `native` with Jobs running, since the
 * apply endpoint doesn't support a `flow` on that install method yet -- a
 * blocked/explanatory state instead of two guaranteed-`409` buttons (see
 * `SelfUpdateModal`'s doc comment). Confirming calls the apply endpoint
 * (`applySelfUpdate`, `POST /api/system/self-update/apply`) with the chosen
 * `flow`; declining (any of the modal's close paths) calls nothing, leaving
 * the app untouched. A successful apply re-execs/exits the server process
 * (see `applySelfUpdate`'s doc comment) -- this page shows a plain "trigger
 * accepted" notice on success rather than trying to track the restart
 * itself; the dedicated polling/restart screen (COL-231) owns that.
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
  const [settingsState, setSettingsState] = useState<SettingsLoadState>({ status: "loading" });
  const [checkingJobs, setCheckingJobs] = useState(false);
  const [selfUpdateModal, setSelfUpdateModal] = useState<SelfUpdateModalState>(null);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [updateNowNotice, setUpdateNowNotice] = useState<UpdateNowNotice>(null);

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

  /**
   * Loads the persisted `update_channel` setting once on mount (COL-228) --
   * {@link canOfferSelfUpdate} needs it to gate "Update Now" to a
   * stable-channel update, same one-shot-on-mount shape `QueuePage` uses for
   * its own settings fetch (`fetchSettings`, `api/settings.ts`).
   */
  useEffect(() => {
    let cancelled = false;
    fetchSettings()
      .then((settings) => {
        if (!cancelled) {
          setSettingsState({ status: "ready", updateChannel: settings.update_channel });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setSettingsState({
            status: "error",
            message: error instanceof Error ? error.message : "Failed to load settings.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * "Update Now" (COL-228): resolves the currently-`running` Job count
   * (`GET /api/jobs/queue`, mirroring `QueuePage`'s own use of
   * `fetchJobQueue`) *before* opening {@link SelfUpdateModal}, so the modal
   * always already knows which of its two shapes to render instead of
   * re-deriving it itself. A failure here surfaces via `updateNowNotice`
   * and leaves the modal closed -- proceeding without knowing the running
   * count would risk silently offering the plain-confirm path when Jobs are
   * in fact running.
   */
  async function handleUpdateNowClick() {
    setCheckingJobs(true);
    setUpdateNowNotice(null);
    try {
      const queue = await fetchJobQueue();
      const runningJobCount = queue.filter((entry) => entry.status === "running").length;
      setSelfUpdateModal({ runningJobCount });
    } catch (error: unknown) {
      setUpdateNowNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "Failed to check running jobs.",
      });
    } finally {
      setCheckingJobs(false);
    }
  }

  /**
   * Confirming the Self-Update modal (COL-228): calls the apply endpoint
   * with the chosen `flow` (`undefined` for the plain-confirm, no-Jobs-
   * running case). A responded failure (`403`/`409`/`502`) surfaces inside
   * the still-open modal via `applyError`, so the operator can see why and
   * retry/cancel; success closes the modal and surfaces a page-level notice
   * instead (see the component doc comment for why this doesn't try to
   * track the restart itself).
   */
  async function handleConfirmSelfUpdate(flow?: SelfUpdateFlow) {
    setApplying(true);
    setApplyError(null);
    try {
      await applySelfUpdate(flow);
      setSelfUpdateModal(null);
      setUpdateNowNotice({
        tone: "success",
        text: "Update triggered. Collapsarr will restart shortly.",
      });
    } catch (error: unknown) {
      setApplyError(error instanceof Error ? error.message : "Failed to trigger the update.");
    } finally {
      setApplying(false);
    }
  }

  /** Closes the Self-Update modal without calling the apply endpoint (COL-228). */
  function handleCloseSelfUpdateModal() {
    setSelfUpdateModal(null);
    setApplyError(null);
  }

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

          {updateNowNotice && (
            <p
              className={updateNowNotice.tone === "error" ? "view__error" : "view__notice"}
              role="status"
            >
              {updateNowNotice.text}
            </p>
          )}

          {state.state.update_available &&
            (canOfferSelfUpdate(state.state, settingsState) ? (
              <div className="update-panel__self-update">
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void handleUpdateNowClick()}
                  disabled={checkingJobs}
                >
                  {checkingJobs ? "Checking…" : "Update Now"}
                </button>
              </div>
            ) : (
              <UpdateInstructions
                installMethod={state.state.install_method}
                latestVersion={state.state.latest_version}
              />
            ))}
        </div>
      )}

      {selfUpdateModal && state.status === "ready" && (
        <SelfUpdateModal
          runningJobCount={selfUpdateModal.runningJobCount}
          installMethod={state.state.install_method}
          applying={applying}
          error={applyError}
          onConfirm={(flow) => void handleConfirmSelfUpdate(flow)}
          onClose={handleCloseSelfUpdateModal}
        />
      )}
    </section>
  );
}
