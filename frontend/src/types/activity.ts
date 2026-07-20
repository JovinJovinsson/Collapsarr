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
