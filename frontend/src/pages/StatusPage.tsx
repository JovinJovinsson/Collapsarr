import { HeartPulse, Info, OctagonAlert, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import { fetchHealthChecks } from "../api/health";
import { fetchSystemInfo } from "../api/system";
import { prefixPath } from "../runtime/urlBase";
import type { HealthCheckState, HealthSeverity } from "../types/health";
import type { SystemInfo } from "../types/system";
import { formatBytes } from "../utils/format";

/** Icon per severity, matching `HealthBanner`/`HealthChecksPage`'s convention. */
const SEVERITY_ICON: Record<HealthSeverity, LucideIcon> = {
  warning: TriangleAlert,
  error: OctagonAlert,
};

/** The static "More Info" external links (COL-125) -- no backend source, so fixed here. */
const SOURCE_URL = "https://github.com/JovinJovinsson/Collapsarr";
const REPORT_ISSUE_URL = "https://github.com/JovinJovinsson/Collapsarr/issues/new";
const COMMUNITY_URL = "https://www.reddit.com/r/Collapsarr/";

/** Formats a duration in seconds as e.g. "2d 3h 12m". */
function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 60) return "Less than a minute";
  const totalMinutes = Math.floor(seconds / 60);
  const days = Math.floor(totalMinutes / (60 * 24));
  const hours = Math.floor((totalMinutes % (60 * 24)) / 60);
  const minutes = totalMinutes % 60;
  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0 || parts.length === 0) parts.push(`${minutes}m`);
  return parts.join(" ");
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; info: SystemInfo };

type HealthLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; failing: HealthCheckState[] };

/** A titled card wrapping one Status-page section (About/Health/More Info). */
function StatusSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="panel status-panel">
      <h2 className="status-panel__title">{title}</h2>
      {children}
    </div>
  );
}

/** One About-panel `dt`/`dd` field row -- extracted since the panel lists a dozen fields. */
function AboutRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="status-panel__row">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/**
 * The System → Status view (COL-123, extended COL-125): three sections --
 * "About" (Collapsarr's runtime/environment facts, sourced from
 * `GET /api/system/info`), "Health" (a compact summary of currently-failing
 * checks), and "More Info" (static external links).
 *
 * The Health section reuses `GET /api/system/health-checks`
 * (`fetchHealthChecks`, `collapsarr/health/routes.py`) -- the same full-detail
 * endpoint `HealthChecksPage` lists -- filtered client-side to currently-
 * failing, non-dismissed rows, mirroring
 * `collapsarr.health.service.list_failing_checks`'s semantics (the same rows
 * the app-wide `HealthBanner` would show). Unlike `HealthChecksPage`, this is
 * a read-only glance: no dismiss controls, and passing/dismissed checks are
 * omitted entirely rather than shown greyed out.
 *
 * Follows the same fetch-on-mount `LoadState` pattern as `TasksPage`
 * (COL-122). The About and Health fetches are independent (separate
 * `useEffect`s/states) so one section's failure doesn't block the other from
 * rendering.
 */
export function StatusPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [healthState, setHealthState] = useState<HealthLoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchSystemInfo()
      .then((info) => {
        if (!cancelled) {
          setState({ status: "ready", info });
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

  useEffect(() => {
    let cancelled = false;
    fetchHealthChecks()
      .then((checks) => {
        if (!cancelled) {
          // Same semantics as collapsarr.health.service.list_failing_checks:
          // currently-failing and not (currently) dismissed.
          const failing = checks.filter(
            (check) => check.status === "failing" && check.dismissed_at === null
          );
          setHealthState({ status: "ready", failing });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setHealthState({
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
      <header className="view__header view__header--row">
        <div>
          <h1 className="view__title">Status</h1>
          <p className="view__summary">
            Runtime and environment facts about this Collapsarr instance.
          </p>
        </div>
      </header>

      <div className="status-page__sections">
        {state.status === "loading" && (
          <div className="panel panel--empty">
            <p className="panel__message">Loading system info…</p>
          </div>
        )}

        {state.status === "error" && (
          <div className="panel panel--empty">
            <span className="panel__icon" aria-hidden>
              <Info width={28} height={28} />
            </span>
            <p className="panel__message">Couldn&apos;t load system info: {state.message}</p>
          </div>
        )}

        {state.status === "ready" && (
          <StatusSection title="About">
            <dl className="status-panel__grid">
              <AboutRow term="Version">{state.info.app_version}</AboutRow>
              <AboutRow term="Install method">{state.info.install_method}</AboutRow>
              <AboutRow term="Python version">{state.info.python_version}</AboutRow>
              <AboutRow term="FFmpeg version">{state.info.ffmpeg_version ?? "Not found"}</AboutRow>
              <AboutRow term="OS">{state.info.os}</AboutRow>
              <AboutRow term="Database engine">{state.info.db_engine}</AboutRow>
              <AboutRow term="Schema revision">{state.info.db_schema_revision ?? "—"}</AboutRow>
              <AboutRow term="Data directory">{state.info.data_dir}</AboutRow>
              <AboutRow term="Database path">{state.info.database_path}</AboutRow>
              <AboutRow term="Uptime">{formatUptime(state.info.uptime_seconds)}</AboutRow>
              <AboutRow term="Timezone">{state.info.timezone}</AboutRow>
              <AboutRow term="Disk usage">
                {formatBytes(state.info.disk.free_bytes)} free of{" "}
                {formatBytes(state.info.disk.total_bytes)}
              </AboutRow>
            </dl>
          </StatusSection>
        )}

        <StatusSection title="Health">
          {healthState.status === "loading" && <p className="panel__message">Loading health checks…</p>}

          {healthState.status === "error" && (
            <p className="panel__message">
              Couldn&apos;t load health checks: {healthState.message}
            </p>
          )}

          {healthState.status === "ready" && healthState.failing.length === 0 && (
            <p className="status-health-clear">
              <HeartPulse width={18} height={18} aria-hidden />
              All health checks are passing.
            </p>
          )}

          {healthState.status === "ready" && healthState.failing.length > 0 && (
            <ul className="status-health-list">
              {healthState.failing.map((check) => {
                const SeverityIcon = SEVERITY_ICON[check.severity] ?? TriangleAlert;
                return (
                  <li key={check.id} className="status-health-row">
                    <SeverityIcon
                      className={`status-health-row__icon status-health-row__icon--${check.severity}`}
                      width={16}
                      height={16}
                      aria-hidden
                    />
                    <span className="status-health-row__code">{check.code}</span>
                    <span className="status-health-row__message">{check.message}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </StatusSection>

        <StatusSection title="More Info">
          <ul className="status-links">
            <li>
              <a href={SOURCE_URL} target="_blank" rel="noopener noreferrer">
                Source
              </a>
            </li>
            <li>
              <a href={REPORT_ISSUE_URL} target="_blank" rel="noopener noreferrer">
                Report an issue
              </a>
            </li>
            <li>
              <a href={prefixPath("/docs")} target="_blank" rel="noopener noreferrer">
                API documentation
              </a>
            </li>
            <li>
              <a href={COMMUNITY_URL} target="_blank" rel="noopener noreferrer">
                Community
              </a>
            </li>
          </ul>
        </StatusSection>
      </div>
    </section>
  );
}
