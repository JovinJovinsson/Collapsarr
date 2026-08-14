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
  /**
   * Join-order sequence number -- a lower value means "earlier"/"next in
   * line" (COL-163, exposed in the response as of COL-175). `GET
   * /api/jobs/queue` (COL-175, `fetchJobQueue`) orders its pending rows by
   * this field ascending; `QueuePage` (COL-178) relies on the server's
   * ordering rather than re-sorting client-side.
   */
  priority: number;
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

/** Matches `collapsarr.jobs.scheduler.DefaultAudioSkipReason`'s enum values (COL-207). */
export type DefaultAudioSkipReason =
  | "no_preference"
  | "already_correct"
  | "unprobeable"
  | "duplicate";

/**
 * Human-readable explanation for each {@link DefaultAudioSkipReason} (COL-207)
 * -- shared by File Detail's single-file result message and, once COL-208
 * lands, the Library page's bulk-result-summary breakdown, so the wording
 * stays consistent between the two "Set Default Audio Track" entry points.
 */
export const DEFAULT_AUDIO_SKIP_REASON_MESSAGE: Record<DefaultAudioSkipReason, string> = {
  no_preference: "No Preferred Default Audio setting is configured yet.",
  already_correct: "This file's Default Audio Track is already set correctly.",
  unprobeable: "The file's audio streams could not be probed.",
  duplicate: "A job for this file is already queued, running, or was processed too recently.",
};

/**
 * Short, lowercase label for each {@link DefaultAudioSkipReason} (COL-208) --
 * fits inline in a bulk-result count like "2 skipped (duplicate)", unlike
 * {@link DEFAULT_AUDIO_SKIP_REASON_MESSAGE}'s full sentences, which are
 * written for a single-file result paragraph instead. The Library page's
 * bulk "Set Default Audio Track" summary uses this to break its skipped
 * count down per reason.
 */
export const DEFAULT_AUDIO_SKIP_REASON_SHORT_LABEL: Record<DefaultAudioSkipReason, string> = {
  no_preference: "no preference configured",
  already_correct: "already correct",
  unprobeable: "unprobeable",
  duplicate: "duplicate",
};

/**
 * Stable enumeration order for {@link DefaultAudioSkipReason} (COL-208) --
 * drives the order per-reason counts appear in the Library page's bulk
 * "Set Default Audio Track" summary, so repeat runs render deterministically
 * rather than depending on `Map`/object key insertion order of whichever
 * reasons happened to appear in a given response.
 */
export const DEFAULT_AUDIO_SKIP_REASONS: readonly DefaultAudioSkipReason[] = [
  "no_preference",
  "already_correct",
  "unprobeable",
  "duplicate",
];

/**
 * Response for `POST /api/jobs/trigger-default-audio` (COL-155,
 * `SetDefaultAudioTriggerResult`; `skip_reason` COL-207). `enqueued` is
 * `true` with the created `job` when a `SET_DEFAULT_AUDIO` job was queued
 * (`skip_reason` is `null`). It is `false` with `job` `null` when the file
 * was skipped, and `skip_reason` names which {@link DefaultAudioSkipReason}
 * explains why -- no preference configured, a duplicate, unprobeable, or
 * already correct.
 */
export interface SetDefaultAudioTriggerResult {
  enqueued: boolean;
  job: EnqueuedJob | null;
  skip_reason: DefaultAudioSkipReason | null;
}

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
 * `FileSetDefaultAudioResult`; `skip_reason` COL-207). Extends
 * {@link SetDefaultAudioTriggerResult} with the `file_path` identifying
 * which resolved file this result belongs to -- a bulk selection can
 * cascade to several files, so there's no other way to attribute an
 * outcome back to one -- so the two response shapes share one definition
 * of the `enqueued`/`job`/`skip_reason` triple rather than two copies that
 * could drift.
 */
export interface FileSetDefaultAudioResult extends SetDefaultAudioTriggerResult {
  file_path: string;
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

/**
 * Request body for `POST /api/jobs/requeue` (COL-170, `RequeueFileRequest`)
 * -- `HistoryPage`'s (COL-176) per-row "Requeue" action on a `failed` row.
 *
 * Unlike {@link ManualTriggerRequest} there is no `extra_languages` option --
 * a requeue retries against the standing language allow-list, not a one-off
 * widened one.
 */
export interface RequeueFileRequest {
  file_path: string;
}

/**
 * Response for `POST /api/jobs/requeue` (COL-170, `RequeueFileResult`). Same
 * shape as {@link ManualTriggerResult}: `enqueued` is `true` with the created
 * `job` when a downmix job was queued, `false` with `job` `null` when the
 * file was skipped -- a duplicate (already `PENDING`/`RUNNING`), unprobeable,
 * or with no qualifying target. The Recently-Processed Window is always
 * bypassed for a requeue, so it is never the skip reason.
 */
export type RequeueFileResult = ManualTriggerResult;

/**
 * Response for `POST /api/jobs/requeue-failed` (COL-172, `BulkRequeueFailedResult`)
 * -- `HistoryPage`'s (COL-179) page-level "Requeue all failed" action, the
 * batch counterpart of {@link RequeueFileResult}'s per-row scope.
 *
 * `requeued` lists every newly created job for a currently-`failed` file this
 * pass did not skip. `skipped` lists every currently-`failed` file's path
 * this pass did not requeue -- most commonly because its most recent
 * terminal history row falls inside the Recently-Processed Window (COL-167),
 * but also any other reason a trigger might decline a file (already active,
 * unprobeable, or nothing left to do). Every currently-failed file lands in
 * exactly one of the two lists -- never a silent partial success, so an
 * all-skipped response is a valid, fully-reported outcome, not an error.
 */
export interface BulkRequeueFailedResult {
  requeued: EnqueuedJob[];
  skipped: string[];
}

/**
 * Response for `DELETE /api/jobs/{job_id}` (COL-168, `CancelJobResult`) --
 * `QueuePage`'s (COL-180) per-row "Cancel" action.
 *
 * `cancelled` is `true` when the Job was still `pending` and has now been
 * removed from the live queue (its `JobHistory` row deleted too -- a
 * cancelled Job leaves no trace). It is `false` -- not an error -- when a
 * worker already claimed the Job, or it already reached a terminal status,
 * before the request landed: "too late" to cancel, distinct from a generic
 * failure; the Job runs (or has run) to completion unaffected. A `job_id`
 * not present in the live queue at all is a `404` instead, surfaced by
 * `api/activity.ts`'s `cancelJob` as a thrown error, not this shape.
 */
export interface CancelJobResult {
  cancelled: boolean;
}

/**
 * Response for `POST /api/jobs/{job_id}/bump` (COL-169, `BumpJobResult`) --
 * `QueuePage`'s (COL-180) per-row "Process next" action.
 *
 * `bumped` is `true` when the Job was still `pending` and has now been
 * reassigned a priority ahead of every other currently-pending Job -- it is
 * the very next Job a free worker claims. It is `false` -- not an error --
 * when a worker already claimed the Job, or it already reached a terminal
 * status, before the request landed: "too late" to bump, unaffected.
 * Mirrors {@link CancelJobResult}'s shape exactly. A `job_id` not present
 * in the live queue at all is a `404` instead, surfaced as a thrown error.
 */
export interface BumpJobResult {
  bumped: boolean;
}

/**
 * Request body for `POST /api/jobs/process-now` (COL-229, `ProcessNowRequest`)
 * -- `QueuePage`'s per-row "Process Now" action.
 *
 * `confirm` defaults to `false`: the first request for a file that would
 * push the number of currently-`running` Jobs past the configured
 * Concurrency Limit is answered with {@link ProcessNowResult.needs_confirmation}
 * set and nothing started; re-submit with `confirm: true` to force-start it
 * over the limit anyway.
 */
export interface ProcessNowRequest {
  file_path: string;
  confirm?: boolean;
}

/**
 * Response for `POST /api/jobs/process-now` (COL-229, `ProcessNowResult`) --
 * `QueuePage`'s per-row "Process Now" action.
 *
 * `needs_confirmation: true` (always paired with `enqueued: false`, `job:
 * null`) means starting this Job right now would push the number of
 * currently-`running` Jobs past the configured Concurrency Limit -- nothing
 * was created or started. Resubmit the same request with `confirm: true` to
 * force-start it anyway; declining (simply not resubmitting) leaves the file
 * exactly as it was.
 *
 * Otherwise `needs_confirmation` is `false` and `enqueued` carries the usual
 * meaning: `true` with the acted-on `job` (already `running`) on success,
 * `false` with `job` `null` when the file was skipped -- unprobeable, or no
 * qualifying target.
 */
export interface ProcessNowResult {
  enqueued: boolean;
  job: EnqueuedJob | null;
  needs_confirmation: boolean;
}

/**
 * Response for `POST /api/jobs/clear` (COL-173, `ClearQueueResult`) --
 * `QueuePage`'s (COL-181) page-level "Clear queue" action.
 *
 * `cancelled` is how many currently-`pending` Jobs, out of every one
 * snapshotted when the pass started, were actually cancelled. `already_running`
 * is how many of that same snapshot had already been claimed by a worker (or
 * otherwise progressed past `pending`) by the time their own cancel ran, and
 * so were left alone -- reported rather than silently dropped, so the UI can
 * show the split instead of a single generic success count. Every snapshotted
 * Job lands in exactly one of the two counts; clearing an already-empty queue
 * is a valid `cancelled: 0`/`already_running: 0` response, not an error.
 */
export interface ClearQueueResult {
  cancelled: number;
  already_running: number;
}
