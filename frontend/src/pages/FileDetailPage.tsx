import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { fetchJobHistory, triggerDownmix } from "../api/activity";
import { fetchSettings } from "../api/settings";
import { fetchWantedList } from "../api/wanted";
import { WantedIcon } from "../components/icons";
import type { JobHistoryEntry, JobStatus, ManualTriggerResult } from "../types/activity";
import type { GlobalSettings } from "../types/settings";
import type { DownmixTarget, WantedFile } from "../types/wanted";

const TARGET_LABEL: Record<string, string> = {
  stereo: "Stereo",
  "2.1": "2.1",
  "5.1": "5.1",
};

const STATUS_LABEL: Record<JobStatus, string> = {
  pending: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
};

/** Best-effort display title from a file path: last segment, minus extension. */
function titleFromPath(filePath: string): string {
  const base = filePath.split(/[/\\]/).pop() || filePath;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

type FileLoadState =
  | { status: "loading" }
  | { status: "not-found" }
  | { status: "error"; message: string }
  | { status: "ready"; file: WantedFile };

type HistoryLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; entries: JobHistoryEntry[] };

type SettingsLoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; settings: GlobalSettings };

type TriggerState =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "error"; message: string }
  | { status: "result"; result: ManualTriggerResult };

interface StatusRow {
  key: string;
  language: string;
  target: DownmixTarget | string;
  label: string;
  tone: "missing" | JobStatus;
}

/**
 * Combines this file's still-missing `(language, target)` pairs (from
 * `GET /api/wanted`) with its job history's latest attempt per pair, so one
 * table shows the current status of every target/language combo this file
 * is either still missing or has a recorded job attempt for.
 *
 * Job history (COL-29's `list_job_history`) is ordered oldest-to-newest, so
 * the last entry per `(language, target)` key -- applied after the
 * "missing" rows are seeded -- is that pair's most recent attempt.
 */
function buildStatusRows(file: WantedFile, history: JobHistoryEntry[]): StatusRow[] {
  const rows = new Map<string, StatusRow>();

  for (const missing of file.missing_targets) {
    const key = `${missing.language}::${missing.target}`;
    rows.set(key, {
      key,
      language: missing.language,
      target: missing.target,
      label: "Missing",
      tone: "missing",
    });
  }

  for (const entry of history) {
    if (!entry.target || !entry.language) continue;
    const key = `${entry.language}::${entry.target}`;
    rows.set(key, {
      key,
      language: entry.language,
      target: entry.target,
      label: STATUS_LABEL[entry.status],
      tone: entry.status,
    });
  }

  return Array.from(rows.values()).sort((a, b) => a.key.localeCompare(b.key));
}

/**
 * The per-file detail view (COL-34): shows one tracked file's current
 * per-target/per-language downmix status and exposes a manual "Trigger
 * downmix" action (`POST /api/jobs/trigger`, COL-29) -- primarily meant for
 * languages the global language allow-list excludes.
 *
 * Those excluded languages never appear as "missing" -- `GET /api/wanted`
 * (COL-28) only returns pairs with status `missing`, and a language outside
 * the allow-list gets the distinct `excluded_by_language_filter` status
 * instead (see `collapsarr.media.service.upsert_tracked_media`), which no
 * endpoint currently surfaces per-file. So rather than inventing a listing
 * this page can't back with real data, the trigger form takes a free-text
 * language code: the backend's `trigger_file` re-probes the file live and
 * decides whether the bypass qualifies, so the frontend doesn't need to
 * already know the file's excluded languages ahead of time.
 *
 * Sourced from `GET /api/wanted` (COL-28, matched by `fileId` -- there's no
 * dedicated per-file detail endpoint yet), `GET /api/jobs/history?file=`
 * (COL-29, this file's past job runs), and `GET /api/settings` (COL-28, to
 * display the current language allow-list for context).
 */
export function FileDetailPage() {
  const { fileId } = useParams<{ fileId: string }>();

  const [fileState, setFileState] = useState<FileLoadState>({ status: "loading" });
  const [historyState, setHistoryState] = useState<HistoryLoadState>({ status: "loading" });
  const [settingsState, setSettingsState] = useState<SettingsLoadState>({ status: "loading" });
  const [triggerState, setTriggerState] = useState<TriggerState>({ status: "idle" });
  const [extraLanguages, setExtraLanguages] = useState("");

  useEffect(() => {
    let cancelled = false;
    setFileState({ status: "loading" });

    fetchWantedList()
      .then((files) => {
        if (cancelled) return;
        const match = files.find((candidate) => String(candidate.id) === fileId);
        setFileState(match ? { status: "ready", file: match } : { status: "not-found" });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setFileState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [fileId]);

  const filePath = fileState.status === "ready" ? fileState.file.file_path : null;

  useEffect(() => {
    if (!filePath) return;
    let cancelled = false;
    setHistoryState({ status: "loading" });

    fetchJobHistory(filePath)
      .then((entries) => {
        if (!cancelled) setHistoryState({ status: "ready", entries });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setHistoryState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [filePath]);

  useEffect(() => {
    let cancelled = false;

    fetchSettings()
      .then((settings) => {
        if (!cancelled) setSettingsState({ status: "ready", settings });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setSettingsState({
            status: "error",
            message: error instanceof Error ? error.message : "Unknown error.",
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const statusRows = useMemo(() => {
    if (fileState.status !== "ready") return [];
    const entries = historyState.status === "ready" ? historyState.entries : [];
    return buildStatusRows(fileState.file, entries);
  }, [fileState, historyState]);

  async function handleTrigger() {
    if (fileState.status !== "ready") return;
    const languages = extraLanguages
      .split(",")
      .map((value) => value.trim())
      .filter((value) => value.length > 0);

    setTriggerState({ status: "submitting" });
    try {
      const result = await triggerDownmix({
        file_path: fileState.file.file_path,
        extra_languages: languages,
      });
      setTriggerState({ status: "result", result });
    } catch (error: unknown) {
      setTriggerState({
        status: "error",
        message: error instanceof Error ? error.message : "Unknown error.",
      });
    }
  }

  return (
    <section className="view">
      <header className="view__header">
        <p className="file-detail__back">
          <Link to="/wanted">&larr; Back to Wanted</Link>
        </p>
        <h1 className="view__title">
          {fileState.status === "ready" ? titleFromPath(fileState.file.file_path) : "File detail"}
        </h1>
        {fileState.status === "ready" && (
          <p className="view__summary file-detail__path">{fileState.file.file_path}</p>
        )}
      </header>

      {fileState.status === "loading" && (
        <div className="panel panel--empty">
          <p className="panel__message">Loading file…</p>
        </div>
      )}

      {fileState.status === "error" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <WantedIcon width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load this file: {fileState.message}</p>
        </div>
      )}

      {fileState.status === "not-found" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <WantedIcon width={28} height={28} />
          </span>
          <p className="panel__message">
            No tracked file with this id is currently in the wanted list. It may already have every
            enabled target processed, or the id may be invalid.
          </p>
        </div>
      )}

      {fileState.status === "ready" && (
        <>
          <div className="panel file-detail__panel">
            <h2 className="settings-form__subtitle">Per-target / per-language status</h2>
            {statusRows.length === 0 ? (
              <p className="panel__message">No target/language status recorded for this file yet.</p>
            ) : (
              <table className="wanted-table">
                <thead>
                  <tr>
                    <th scope="col">Language</th>
                    <th scope="col">Target</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {statusRows.map((row) => (
                    <tr key={row.key}>
                      <td>{row.language}</td>
                      <td>{TARGET_LABEL[row.target] ?? row.target}</td>
                      <td>
                        <span className={`activity-table__status activity-table__status--${row.tone}`}>
                          {row.label}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {historyState.status === "error" && (
              <p className="form-error">Couldn&apos;t load job history: {historyState.message}</p>
            )}
          </div>

          <div className="panel file-detail__panel">
            <h2 className="settings-form__subtitle">Trigger downmix</h2>
            <p className="panel__message file-detail__hint">
              {settingsState.status === "ready" &&
                (settingsState.settings.language_allow_list ? (
                  <>
                    Global language allow-list:{" "}
                    <strong>{settingsState.settings.language_allow_list.join(", ")}</strong>. A
                    language outside this list is skipped automatically — enter its code below to
                    force a job for it anyway.
                  </>
                ) : (
                  "No language allow-list is set — every language is already eligible."
                ))}
              {settingsState.status === "error" && `Couldn't load settings: ${settingsState.message}`}
              {settingsState.status === "loading" && "Loading language allow-list…"}
            </p>

            <div className="form-field form-field--narrow">
              <label htmlFor="extra-languages">Bypass language allow-list (comma-separated codes)</label>
              <input
                id="extra-languages"
                type="text"
                placeholder="e.g. de, ja"
                value={extraLanguages}
                onChange={(event) => setExtraLanguages(event.target.value)}
              />
            </div>

            <div className="form-actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={handleTrigger}
                disabled={triggerState.status === "submitting"}
              >
                {triggerState.status === "submitting" ? "Triggering…" : "Trigger downmix"}
              </button>
            </div>

            {triggerState.status === "error" && (
              <p className="form-error">Couldn&apos;t trigger downmix: {triggerState.message}</p>
            )}

            {triggerState.status === "result" && (
              <p className={triggerState.result.enqueued ? "form-success" : "form-hint"}>
                {triggerState.result.enqueued && triggerState.result.job ? (
                  <>
                    Job <code>{triggerState.result.job.id}</code> enqueued —{" "}
                    <span
                      className={`activity-table__status activity-table__status--${triggerState.result.job.status}`}
                    >
                      {STATUS_LABEL[triggerState.result.job.status]}
                    </span>
                  </>
                ) : (
                  "No job enqueued — the file was skipped (already queued, unprobeable, or nothing qualifying even with the bypass)."
                )}
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}
