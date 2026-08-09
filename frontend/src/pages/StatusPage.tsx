import { useEffect, useState } from "react";

import { fetchSystemInfo } from "../api/system";
import { StatusIcon } from "../components/icons";
import type { SystemInfo } from "../types/system";
import { formatBytes } from "../utils/format";

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

/**
 * The System → Status view (COL-123): an "About" panel of Collapsarr's
 * runtime/environment facts -- app version, install method, Python/FFmpeg/OS
 * versions, the configured database engine and its live schema revision,
 * data paths, process uptime, timezone, and data-dir disk usage -- sourced
 * from `GET /api/system/info` (`fetchSystemInfo`, `collapsarr/system/info.py`).
 *
 * Follows the same fetch-on-mount `LoadState` pattern as `TasksPage`
 * (COL-122): a loading message, an error panel, and -- once loaded -- a
 * definition list of every About field. A health-check summary and
 * "more info" links are deliberately out of scope here; COL-125 adds those
 * to this same page.
 */
export function StatusPage() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

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

      {state.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading system info…</p>
        </div>
      )}

      {state.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <StatusIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load system info: {state.message}</p>
        </div>
      )}

      {state.status === "ready" && (
        <div className="panel status-panel">
          <h2 className="status-panel__title">About</h2>
          <dl className="status-panel__grid">
            <div className="status-panel__row">
              <dt>Version</dt>
              <dd>{state.info.app_version}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Install method</dt>
              <dd>{state.info.install_method}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Python version</dt>
              <dd>{state.info.python_version}</dd>
            </div>
            <div className="status-panel__row">
              <dt>FFmpeg version</dt>
              <dd>{state.info.ffmpeg_version ?? "Not found"}</dd>
            </div>
            <div className="status-panel__row">
              <dt>OS</dt>
              <dd>{state.info.os}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Database engine</dt>
              <dd>{state.info.db_engine}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Schema revision</dt>
              <dd>{state.info.db_schema_revision ?? "—"}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Data directory</dt>
              <dd>{state.info.data_dir}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Database path</dt>
              <dd>{state.info.database_path}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Uptime</dt>
              <dd>{formatUptime(state.info.uptime_seconds)}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Timezone</dt>
              <dd>{state.info.timezone}</dd>
            </div>
            <div className="status-panel__row">
              <dt>Disk usage</dt>
              <dd>
                {formatBytes(state.info.disk.free_bytes)} free of{" "}
                {formatBytes(state.info.disk.total_bytes)}
              </dd>
            </div>
          </dl>
        </div>
      )}
    </section>
  );
}
