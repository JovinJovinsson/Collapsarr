import { CakeSlice } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { fetchJobHistory, triggerDownmix, triggerSetDefaultAudio } from "../api/activity";
import { FileNotFoundError, fetchFileById } from "../api/files";
import { updateTracked } from "../api/library";
import { fetchSettings } from "../api/settings";
import { TrackedToggleButton } from "../components/TrackedToggleButton";
import { JOB_KIND_LABEL } from "../types/activity";
import type { JobHistoryEntry, JobKind, JobStatus, ManualTriggerResult } from "../types/activity";
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
  /** The job kind (COL-155) that produced this row's latest attempt, or `null` for a still-"Missing" row with no recorded attempt yet. */
  kind: JobKind | null;
}

/**
 * Combines this file's still-missing `(language, target)` pairs (from
 * `GET /api/files/:id`) with its job history's latest attempt per pair, so
 * one table shows the current status of every target/language combo this
 * file is either still missing or has a recorded job attempt for.
 *
 * Job history (COL-29's `list_job_history`) is ordered oldest-to-newest, so
 * the last entry per `(language, target)` key -- applied after the
 * "missing" rows are seeded -- is that pair's most recent attempt. A row's
 * `kind` (COL-155/COL-157) reflects that latest attempt's job kind -- both a
 * `DOWNMIX` job's enabled targets and a `SET_DEFAULT_AUDIO` job's resolved
 * preference land in the same `(language, target)` key space (see
 * `collapsarr.jobs.history._target`/`_language`), so the kind label is what
 * lets a reader tell the two apart.
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
      kind: null,
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
      kind: entry.kind,
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
 * Sourced from `GET /api/files/:id` (COL-203, this file resolved by id
 * independent of Wanted-queue membership -- so a fully-processed file with
 * no missing targets still opens here), `GET /api/jobs/history?file=`
 * (COL-29, this file's past job runs), and `GET /api/settings` (COL-28, to
 * display the current language allow-list for context).
 *
 * Also shows and toggles this file's **Tracked** status (COL-101), bridged
 * from `GET /api/files/:id`'s `library_node_id`/`node_type`/`tracked` fields
 * (`collapsarr/media/routes.py`'s bridge from a tracked-media row back to
 * its owning `LibraryNode`, shared with `GET /api/wanted`). When the bridge
 * hasn't resolved yet (no instance/episode/movie id captured for this
 * file), the panel shows a "status unavailable" message rather than a
 * broken toggle -- see that module's docstring for when this happens.
 *
 * Also exposes a manual "Set Default Audio Track" action (COL-157,
 * `POST /api/jobs/trigger-default-audio`, COL-155), enqueuing a
 * `SET_DEFAULT_AUDIO` job for this file against the current global
 * Preferred Default Audio setting -- unlike "Trigger downmix" it takes no
 * override input, since that fix isn't gated by a language allow-list. The
 * per-target/per-language status table's new "Kind" column (COL-155's job
 * history `kind` field) distinguishes a row produced by a `DOWNMIX` job from
 * one produced by a `SET_DEFAULT_AUDIO` job, since both can land in the same
 * `(language, target)` key (see `buildStatusRows`).
 */
export function FileDetailPage() {
  const { fileId } = useParams<{ fileId: string }>();

  const [fileState, setFileState] = useState<FileLoadState>({ status: "loading" });
  const [historyState, setHistoryState] = useState<HistoryLoadState>({ status: "loading" });
  const [settingsState, setSettingsState] = useState<SettingsLoadState>({ status: "loading" });
  const [triggerState, setTriggerState] = useState<TriggerState>({ status: "idle" });
  const [defaultAudioTriggerState, setDefaultAudioTriggerState] = useState<TriggerState>({
    status: "idle",
  });
  const [extraLanguages, setExtraLanguages] = useState("");
  const [trackedPending, setTrackedPending] = useState(false);
  const [trackedError, setTrackedError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFileState({ status: "loading" });

    if (!fileId) {
      setFileState({ status: "not-found" });
      return;
    }

    fetchFileById(fileId)
      .then((file) => {
        if (!cancelled) setFileState({ status: "ready", file });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof FileNotFoundError) {
          setFileState({ status: "not-found" });
          return;
        }
        setFileState({
          status: "error",
          message: error instanceof Error ? error.message : "Unknown error.",
        });
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

  /**
   * Manually enqueues a `SET_DEFAULT_AUDIO` job for this file (COL-155's
   * `POST /api/jobs/trigger-default-audio`, COL-157's "Set Default Audio
   * Track" action). Mirrors `handleTrigger` above -- same
   * submitting/error/result state shape and result-display pattern -- minus
   * the language-bypass input, which has no analogue here (the Default
   * Audio Track fix isn't gated by a language allow-list).
   */
  async function handleTriggerDefaultAudio() {
    if (fileState.status !== "ready") return;

    setDefaultAudioTriggerState({ status: "submitting" });
    try {
      const result = await triggerSetDefaultAudio({ file_path: fileState.file.file_path });
      setDefaultAudioTriggerState({ status: "result", result });
    } catch (error: unknown) {
      setDefaultAudioTriggerState({
        status: "error",
        message: error instanceof Error ? error.message : "Unknown error.",
      });
    }
  }

  /**
   * Toggles this file's bridged Library node's Tracked value (COL-101),
   * calling the same `POST /api/library/tracked` endpoint LibraryPage's row
   * toggles do, with this file's `library_node_id`/`node_type` as the single
   * reference. Unlike LibraryPage's toggle, no refetch-the-tree dance is
   * needed afterwards: this file's bridged node is always an Episode/Movie
   * leaf (never a Series/Season), so there's no cascade to reflect --
   * patching `fileState` directly from the response's resulting value is
   * both correct and avoids a redundant round-trip.
   */
  async function handleTrackedToggle() {
    if (fileState.status !== "ready") return;
    const file = fileState.file;
    if (file.library_node_id === null || file.node_type === null) return;
    const nextTracked = !file.tracked;

    setTrackedError(null);
    setTrackedPending(true);
    try {
      const result = await updateTracked(file.node_type, file.library_node_id, nextTracked);
      const updated = result.updated.find((entry) => entry.id === file.library_node_id);
      setFileState({
        status: "ready",
        file: { ...file, tracked: updated ? updated.tracked : nextTracked },
      });
    } catch (error: unknown) {
      setTrackedError(error instanceof Error ? error.message : "Unknown error.");
    } finally {
      setTrackedPending(false);
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
            <CakeSlice width={28} height={28} />
          </span>
          <p className="panel__message">Couldn&apos;t load this file: {fileState.message}</p>
        </div>
      )}

      {fileState.status === "not-found" && (
        <div className="panel panel--empty">
          <span className="panel__icon" aria-hidden>
            <CakeSlice width={28} height={28} />
          </span>
          <p className="panel__message">
            No tracked file exists with this id. It may have been removed, or the id may be invalid.
          </p>
        </div>
      )}

      {fileState.status === "ready" && (
        <>
          <div className="panel file-detail__panel">
            <h2 className="settings-form__subtitle">Tracked</h2>
            {fileState.file.library_node_id !== null &&
            fileState.file.node_type !== null &&
            fileState.file.tracked !== null ? (
              <>
                <p className="panel__message file-detail__hint">
                  Tracked gates automatic downmixing for this file. A Not-Tracked file can still be
                  downmixed via the manual trigger below.
                </p>
                <TrackedToggleButton
                  tracked={fileState.file.tracked}
                  pending={trackedPending}
                  onToggle={handleTrackedToggle}
                />
              </>
            ) : (
              <p className="panel__message">
                Tracked status is unavailable for this file (it hasn&apos;t been linked back to its
                library entry yet — try rescanning).
              </p>
            )}
            {trackedError && (
              <p className="form-error">Couldn&apos;t update Tracked: {trackedError}</p>
            )}
          </div>

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
                    <th scope="col">Kind</th>
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
                      <td>
                        {row.kind ? (
                          <span className={`activity-table__kind activity-table__kind--${row.kind}`}>
                            {JOB_KIND_LABEL[row.kind]}
                          </span>
                        ) : (
                          "—"
                        )}
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

          <div className="panel file-detail__panel">
            <h2 className="settings-form__subtitle">Set Default Audio Track</h2>
            <p className="panel__message file-detail__hint">
              Swaps this file&apos;s Default Audio Track disposition onto the track matching the
              current Preferred Default Audio setting, if it isn&apos;t already set correctly.
            </p>

            <div className="form-actions">
              <button
                type="button"
                className="btn btn--primary"
                onClick={handleTriggerDefaultAudio}
                disabled={defaultAudioTriggerState.status === "submitting"}
              >
                {defaultAudioTriggerState.status === "submitting"
                  ? "Setting…"
                  : "Set Default Audio Track"}
              </button>
            </div>

            {defaultAudioTriggerState.status === "error" && (
              <p className="form-error">
                Couldn&apos;t trigger Set Default Audio Track: {defaultAudioTriggerState.message}
              </p>
            )}

            {defaultAudioTriggerState.status === "result" && (
              <p className={defaultAudioTriggerState.result.enqueued ? "form-success" : "form-hint"}>
                {defaultAudioTriggerState.result.enqueued && defaultAudioTriggerState.result.job ? (
                  <>
                    Job <code>{defaultAudioTriggerState.result.job.id}</code> enqueued —{" "}
                    <span
                      className={`activity-table__status activity-table__status--${defaultAudioTriggerState.result.job.status}`}
                    >
                      {STATUS_LABEL[defaultAudioTriggerState.result.job.status]}
                    </span>
                  </>
                ) : (
                  "No job enqueued — the file was skipped (already correct, no preference configured, already queued, or unprobeable)."
                )}
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}
