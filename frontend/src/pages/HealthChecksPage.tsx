import { useEffect, useState } from "react";

import { fetchHealthChecks } from "../api/health";
import { ErrorIcon, HealthIcon, WarningIcon } from "../components/icons";
import type { HealthCheckState, HealthSeverity, HealthCheckStatus } from "../types/health";

const SEVERITY_LABEL: Record<HealthSeverity, string> = {
  warning: "Warning",
  error: "Error",
};

const SEVERITY_ICON = {
  warning: WarningIcon,
  error: ErrorIcon,
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
 * Deliberately minimal for this slice: no dismiss/undismiss (COL-82) or manual
 * recheck (COL-83) actions yet. Each row carries the check state's stable `id`
 * so those later slices can add per-row actions without reshaping this table.
 */
export function HealthChecksPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

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

  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">Health</h1>
        <p className="view__summary">
          Every registered health check and its current state — FFmpeg availability, connectivity,
          and any others the app monitors.
        </p>
      </header>

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading health checks…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <HealthIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load health checks: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && state.checks.length === 0 && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <HealthIcon width={28} height={28} />
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
              </tr>
            </thead>
            <tbody>
              {state.checks.map((check) => {
                const SeverityIcon = SEVERITY_ICON[check.severity] ?? WarningIcon;
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
