import { OctagonAlert, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { downloadFfmpeg } from "../api/ffmpeg";
import { fetchSystemInfo } from "../api/system";
import { useHealth } from "../hooks/useHealth";
import type { HealthWarning } from "../types/health";
import type { InstallMethod } from "../types/system";

/** Icon per severity -- error gets the octagon, warning the triangle (COL-76). */
const SEVERITY_ICON: Record<HealthWarning["severity"], LucideIcon> = {
  warning: TriangleAlert,
  error: OctagonAlert,
};

/** The Check Code the opt-in "Download FFmpeg" action attaches to (COL-222). */
const FFMPEG_MISSING_CODE = "ffmpeg_missing";

/**
 * App-wide health warning banner (COL-38). Reads the shared `GET /health`
 * state and, when the app reports itself "degraded" (one or more registered
 * checks currently failing, `collapsarr/health/`), renders a persistent
 * banner above every view -- rendered in `AppShell` so it's visible
 * regardless of which page the user is on. Renders nothing when the app is
 * healthy, when the fetch hasn't resolved yet, or if the fetch itself fails
 * (a transient network hiccup shouldn't itself read as an alarming health
 * warning).
 *
 * COL-76 distinguishes each entry's severity (`warning` vs `error`, per
 * `/health`'s `severity` field) with a different icon/colour -- previously
 * every entry rendered identically regardless of how many checks were failing
 * or how severe each one was. A single mixed-severity list still renders as
 * one banner (not one per severity) so it stays a single glance-able summary;
 * only the per-row icon/colour differs.
 *
 * COL-124 code review: reads the shared `GET /health` state from
 * `HealthProvider` via `useHealth()` instead of fetching its own copy --
 * `Sidebar`'s version footer reads the same shared state, so the app makes
 * that request once rather than twice.
 *
 * COL-222 adds an opt-in "Download FFmpeg" action to the `ffmpeg_missing`
 * row specifically (`POST /api/system/ffmpeg/download`, `api/ffmpeg.ts`):
 * never triggered automatically (ADR 0001/0002), and hidden entirely unless
 * `GET /api/system/info`'s `install_method` is positively known to be
 * `"native"` or `"pipx"` -- fetched lazily, only once this warning is
 * actually showing, and fails closed (stays hidden) on a fetch error or an
 * unexpected value, since Docker already bundles FFmpeg and must never show
 * this action. On success, `refresh()` (from `HealthProvider`, COL-222)
 * re-fetches `/health` immediately so the row clears without a page reload,
 * since COL-218's health check already reads the freshly-persisted
 * `ffmpeg_path` on its very next tick. On failure, the server's error is
 * shown inline and the button re-enables so the operator can retry.
 */
export function HealthBanner() {
  const { state, refresh } = useHealth();
  const [installMethod, setInstallMethod] = useState<InstallMethod | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // `state.health.warnings` is guarded with `?? []` rather than assumed
  // present -- some shell-level tests stub a bare, non-`HealthStatus` fetch
  // response for every endpoint including `/health`; this must degrade to
  // "no ffmpeg_missing warning" rather than throw in that case, same as the
  // `health.warnings.length === 0` check below already tolerates via its
  // `health.status !== "degraded"` short-circuit.
  const hasFfmpegMissingWarning =
    state.status === "ready" &&
    (state.health.warnings ?? []).some((warning) => warning.code === FFMPEG_MISSING_CODE);

  useEffect(() => {
    if (!hasFfmpegMissingWarning) return;
    let cancelled = false;
    fetchSystemInfo()
      .then((info) => {
        if (!cancelled) setInstallMethod(info.install_method);
      })
      .catch(() => {
        // Best-effort: install_method stays `null` -- see the docstring's
        // "fails closed" note -- so the action simply never appears rather
        // than risk showing it under a Docker install we failed to detect.
      });
    return () => {
      cancelled = true;
    };
  }, [hasFfmpegMissingWarning]);

  if (state.status !== "ready") {
    // Renders nothing while the fetch hasn't resolved yet, or if it failed
    // (a transient network hiccup shouldn't itself read as an alarming
    // health warning) -- see the docstring above.
    return null;
  }
  const { health } = state;

  if (health.status !== "degraded" || health.warnings.length === 0) {
    return null;
  }

  async function handleDownloadFfmpeg() {
    setDownloading(true);
    setDownloadError(null);
    try {
      await downloadFfmpeg();
      await refresh();
    } catch (error: unknown) {
      setDownloadError(error instanceof Error ? error.message : "Failed to download FFmpeg.");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="health-banner" role="alert">
      {health.warnings.map((warning) => {
        // Defensive default (rather than trusting the value blindly): an
        // unexpected/missing severity -- e.g. a stale cached response from
        // before COL-76 added the field -- still renders, just as a warning.
        const severity: HealthWarning["severity"] = warning.severity === "error" ? "error" : "warning";
        const Icon = SEVERITY_ICON[severity];
        // Only shown once install_method is positively known to be
        // non-Docker -- see this component's docstring.
        const showDownloadAction =
          warning.code === FFMPEG_MISSING_CODE &&
          (installMethod === "native" || installMethod === "pipx");
        return (
          <p key={warning.code} className={`health-banner__message health-banner__message--${severity}`}>
            <Icon className="health-banner__icon" size={20} />
            {warning.message}
            {showDownloadAction && (
              <button
                type="button"
                className="btn btn--secondary btn--sm health-banner__action"
                onClick={handleDownloadFfmpeg}
                disabled={downloading}
              >
                {downloading ? "Downloading…" : "Download FFmpeg"}
              </button>
            )}
          </p>
        );
      })}
      {downloadError && <p className="health-banner__error">{downloadError}</p>}
    </div>
  );
}
