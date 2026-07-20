/**
 * Types mirroring the `/api/jobs/history` response (COL-29,
 * `collapsarr/jobs/routes.py`'s `JobHistoryRead`) -- kept in sync by hand
 * since there's no shared schema generation yet.
 */

/** Matches `collapsarr.jobs.queue.JobStatus`'s enum values. */
export type JobStatus = "pending" | "running" | "succeeded" | "failed";

/** One persisted job-history row: a single downmix job run (COL-21). */
export interface JobHistoryEntry {
  id: number;
  job_id: string;
  file_path: string;
  status: JobStatus;
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
