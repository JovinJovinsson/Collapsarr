/**
 * Types mirroring the `/api/jobs/history` response (COL-29,
 * `collapsarr/jobs/routes.py`'s `JobHistoryRead`) -- kept in sync by hand
 * since there's no shared schema generation yet.
 */

import type { TrackedNodeReference } from "./library";

/** Matches `collapsarr.jobs.queue.JobStatus`'s enum values. */
export type JobStatus = "pending" | "running" | "succeeded" | "failed";

/**
 * Matches `collapsarr.jobs.queue.JobKind`'s enum values (COL-155). `downmix`
 * is the original pipeline and the default for every job/history row that
 * predates this kind (including backfilled historical rows);
 * `set_default_audio` is the manual/bulk Default Audio Track fix (COL-153's
 * disposition-only pipeline).
 */
export type JobKind = "downmix" | "set_default_audio";

/** Display label for a {@link JobKind}, shared by File Detail's and Activity's Job History tables (COL-157). */
export const JOB_KIND_LABEL: Record<JobKind, string> = {
  downmix: "Downmix",
  set_default_audio: "Set Default Audio Track",
};

/** One persisted job-history row: a single downmix or set-default-audio job run (COL-21, COL-155). */
export interface JobHistoryEntry {
  id: number;
  job_id: string;
  file_path: string;
  status: JobStatus;
  kind: JobKind;
  started_at: string | null;
  ended_at: string | null;
  exit_code: number | null;
  error_text: string | null;
  target: string | null;
  language: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Request body for `POST /api/jobs/trigger` (COL-29, `ManualTriggerRequest`).
 *
 * `extra_languages` is the allow-list-bypass option: languages listed here
 * are unioned onto the scheduler's `language_allow_list` for this one call,
 * so a language the global allow-list would otherwise exclude still gets a
 * downmix job. Omit it (or send `[]`) for a plain trigger honouring the
 * allow-list.
 */
export interface ManualTriggerRequest {
  file_path: string;
  extra_languages?: string[];
}

/** The identifying summary of a job the scheduler just enqueued in-memory. */
export interface EnqueuedJob {
  id: string;
  file_path: string;
  status: JobStatus;
}

/**
 * Response for `POST /api/jobs/trigger` (COL-29, `ManualTriggerResult`).
 *
 * `enqueued` is `true` with the created `job` when a downmix job was
 * queued. It is `false` with `job` `null` when the file was skipped -- a
 * duplicate (already queued / recently processed), unprobeable, or with no
 * qualifying target even after `extra_languages`.
 */
export interface ManualTriggerResult {
  enqueued: boolean;
  job: EnqueuedJob | null;
}

/**
 * Request body for `POST /api/jobs/trigger-default-audio` (COL-155,
 * `SetDefaultAudioTriggerRequest`).
 *
 * Unlike {@link ManualTriggerRequest} there is no allow-list-bypass option --
 * the Default Audio Track fix isn't gated by a language allow-list at all.
 */
export interface SetDefaultAudioTriggerRequest {
  file_path: string;
}

/**
 * Response for `POST /api/jobs/trigger-default-audio` (COL-155,
 * `SetDefaultAudioTriggerResult`). Same shape as {@link ManualTriggerResult}:
 * `enqueued` is `true` with the created `job` when a `SET_DEFAULT_AUDIO` job
 * was queued, `false` with `job` `null` when the file was skipped -- no
 * preference configured, a duplicate, unprobeable, or already correct.
 */
export type SetDefaultAudioTriggerResult = ManualTriggerResult;

/**
 * Request body for `POST /api/jobs/trigger-default-audio/bulk` (COL-156,
 * `BulkSetDefaultAudioTriggerRequest`) -- the Library page's bulk "Set
 * Default Audio Track" action (COL-158). `references` reuses
 * {@link TrackedNodeReference} (`types/library.ts`, COL-101) rather than
 * defining an identical type of its own: the backend's own
 * `DefaultAudioNodeReference` is documented as "identical shape to
 * `TrackedNodeReference`" for the same reason
 * (`collapsarr/jobs/routes.py`), and this is the same mixed Series/Season/
 * Episode/Movie selection `POST /api/library/tracked`'s bulk Tracked update
 * already takes.
 */
export interface BulkSetDefaultAudioTriggerRequest {
  references: TrackedNodeReference[];
}

/**
 * One resolved file's outcome within a bulk trigger response (COL-156,
 * `FileSetDefaultAudioResult`). Mirrors {@link SetDefaultAudioTriggerResult}'s
 * `enqueued`/`job` pair, per file, plus the `file_path` identifying which
 * resolved file this result belongs to -- a bulk selection can cascade to
 * several files, so there's no other way to attribute an outcome back to one.
 */
export interface FileSetDefaultAudioResult {
  file_path: string;
  enqueued: boolean;
  job: EnqueuedJob | null;
}

/**
 * Response for `POST /api/jobs/trigger-default-audio/bulk` (COL-156,
 * `BulkSetDefaultAudioTriggerResult`) -- one {@link FileSetDefaultAudioResult}
 * per unique file the request's references resolved to (after the backend's
 * own Series/Season cascade and de-dup).
 */
export interface BulkSetDefaultAudioTriggerResult {
  results: FileSetDefaultAudioResult[];
}
