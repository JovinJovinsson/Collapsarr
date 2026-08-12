import { HeartPulse, OctagonAlert, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";

import {
  dismissHealthCheck,
  fetchHealthChecks,
  recheckHealthChecks,
  undismissHealthCheck,
} from "../api/health";
import type { HealthCheckState, HealthSeverity, HealthCheckStatus } from "../types/health";

const SEVERITY_LABEL: Record<HealthSeverity, string> = {
  warning: "Warning",
  error: "Error",
};

const SEVERITY_ICON: Record<HealthSeverity, LucideIcon> = {
  warning: TriangleAlert,
  error: OctagonAlert,
};

const STATUS_LABEL: Record<HealthCheckStatus, string> = {
  passing: "Passing",
  failing: "Failing",
};

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
  | { status: "ready"; checks: HealthCheckState[] };

/**
 * The System → Health view (COL-76): every registered health check's current
 * state -- code, category, severity, status, message, and timing -- sourced
 * from `GET /api/system/health-checks` (`fetchHealthChecks`,
 * `collapsarr/health/routes.py`). Unlike the app-wide `HealthBanner`, which
 * only ever shows *currently-failing* checks, this page lists every check the
 * framework runs, passing or failing, so an operator can see the full picture
 * (and confirm nothing else is quietly wrong) in one place.
 *
 * Generic over whatever checks are registered
 * (`collapsarr.health.registry.default_health_checks`) -- nothing here is
 * hardcoded to FFmpeg; a per-instance check's rows are told apart by
 * `instance_id`.
 *
 * COL-82 adds per-row "Dismiss"/"Undismiss" actions
 * (`POST /api/system/health-checks/{id}/dismiss` /
 * `.../undismiss`, `api/health.ts`). Dismissing a currently-failing row marks
 * it "Dismissed" here (it never disappears from this page -- only the
 * app-wide banner hides a dismissed check) and reloads the list so the badge
 * reflects the server's state; "Dismiss" is only offered while a row is
 * failing (the server would otherwise refuse with `409`), and "Undismiss"
 * only once it's dismissed.
 *
 * COL-83 adds a page-level "Recheck now" action, calling
 * `POST /api/system/health-checks/recheck` (`recheckHealthChecks`,
 * `api/health.ts`), which runs every registered check immediately
 * server-side (a genuine extra tick -- it does not restart or reset the
 * background scheduler's own periodic cadence). Unlike the per-row
 * dismiss/undismiss actions, the recheck response already carries every
 * check's fresh full state, so it's applied straight to this page's state
 * instead of triggering a second `fetchHealthChecks` round-trip -- still no
 * full page reload, updated results appear as soon as the request resolves.
 */
export function HealthChecksPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [rechecking, setRechecking] = useState(false);

  async function load() {
    try {
      const checks = await fetchHealthChecks();
      setState({ status: "ready", checks });
    } catch (error: unknown) {
      setState({
        status: "error",
        message: error instanceof Error ? error.message : "Unknown error.",
      });
    }
  }

  useEffect(() => {
    let cancelled = false;
    fetchHealthChecks()
      .then((checks) => {
        if (!cancelled) {
          setState({ status: "ready", checks });
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

  async function handleDismiss(check: HealthCheckState) {
    setPendingId(check.id);
    setActionError(null);
    try {
      await dismissHealthCheck(check);
      await load();
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to dismiss health check.");
    } finally {
      setPendingId(null);
    }
  }

  async function handleUndismiss(check: HealthCheckState) {
    setPendingId(check.id);
    setActionError(null);
    try {
      await undismissHealthCheck(check);
      await load();
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to undismiss health check.");
    } finally {
      setPendingId(null);
    }
  }

  async function handleRecheck() {
    setRechecking(true);
    setActionError(null);
    try {
      const checks = await recheckHealthChecks();
      setState({ status: "ready", checks });
    } catch (error: unknown) {
      setActionError(error instanceof Error ? error.message : "Failed to run a manual recheck.");
    } finally {
      setRechecking(false);
    }
  }

  return (
    <section className="view">
      <header className="view__header view__header--row">
        <div>
          <h1 className="view__title">Health</h1>
          <p className="view__summary">
            Every registered health check and its current state — FFmpeg availability, connectivity,
            and any others the app monitors.
          </p>
        </div>
        <div className="view__actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={handleRecheck}
            disabled={rechecking}
          >
            {rechecking ? "Rechecking…" : "Recheck now"}
          </button>
        </div>
      </header>

      {actionError && <p className="view__error">{actionError}</p>}

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading health checks…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <HeartPulse width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load health checks: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.checks.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <HeartPulse width={28} height={28} />
          </span>
          <p className="panel__message">No health checks have run yet.</p>
        </div>
      )}

      {state.status === "ready" && state.checks.length > 0 && (
        <div className="panel">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Code</th>
                <th scope="col">Category</th>
                <th scope="col">Severity</th>
                <th scope="col">Status</th>
                <th scope="col">Message</th>
                <th scope="col">First failed</th>
                <th scope="col">Last checked</th>
                <th scope="col">Dismissed</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {state.checks.map((check) => {
                const SeverityIcon = SEVERITY_ICON[check.severity] ?? TriangleAlert;
                const isDismissed = check.dismissed_at !== null;
                const isPending = pendingId === check.id;
                return (
                  <tr key={check.id}>
                    <td>
                      {check.code}
                      {check.instance_id !== null && (
                        <span className="health-table__instance"> (instance {check.instance_id})</span>
                      )}
                    </td>
                    <td>{check.category}</td>
                    <td>
                      <span
                        className={`health-table__severity health-table__severity--${check.severity}`}
                      >
                        <SeverityIcon className="health-table__severity-icon" width={14} height={14} />
                        {SEVERITY_LABEL[check.severity] ?? check.severity}
                      </span>
                    </td>
                    <td>
                      <span className={`health-table__status health-table__status--${check.status}`}>
                        {STATUS_LABEL[check.status] ?? check.status}
                      </span>
                    </td>
                    <td className="health-table__message">{check.message}</td>
                    <td>{formatTimestamp(check.first_failed_at)}</td>
                    <td>{formatTimestamp(check.last_checked_at)}</td>
                    <td>
                      <span className={`health-table__dismissed${isDismissed ? " health-table__dismissed--yes" : ""}`}>
                        {isDismissed ? "Dismissed" : "—"}
                      </span>
                    </td>
                    <td className="data-table__actions">
                      {check.status === "failing" && !isDismissed && (
                        <button
                          type="button"
                          className="btn btn--secondary btn--sm"
                          onClick={() => handleDismiss(check)}
                          disabled={isPending}
                        >
                          {isPending ? "Dismissing…" : "Dismiss"}
                        </button>
                      )}
                      {isDismissed && (
                        <button
                          type="button"
                          className="btn btn--secondary btn--sm"
                          onClick={() => handleUndismiss(check)}
                          disabled={isPending}
                        >
                          {isPending ? "Undismissing…" : "Undismiss"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
