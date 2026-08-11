import type {
  BulkSetDefaultAudioTriggerRequest,
  BulkSetDefaultAudioTriggerResult,
  BumpJobResult,
  CancelJobResult,
  ClearQueueResult,
  JobHistoryEntry,
  ManualTriggerRequest,
  ManualTriggerResult,
  RequeueFileRequest,
  RequeueFileResult,
  SetDefaultAudioTriggerRequest,
  SetDefaultAudioTriggerResult,
} from "../types/activity";
import type { TrackedNodeReference } from "../types/library";
import { apiErrorMessage, apiFetch } from "./client";

const JSON_HEADERS = { "Content-Type": "application/json" };

/**
 * Fetches persisted job history (`GET /api/jobs/history`, COL-29).
 *
 * The endpoint accepts optional `file` (exact path match) and `status`
 * query params for server-side filtering. The now-repurposed `ActivityPage`
 * (COL-178's `QueuePage`, sourced from {@link fetchJobQueue} instead) used to
 * fetch the full list unfiltered and filter client-side -- the server's
 * `file` filter is exact-match only, a poor fit for an interactive text
 * filter -- and `HistoryPage` (COL-176) is this fetch's remaining consumer,
 * taking over that same fetch-all-and-filter-client-side role for terminal
 * (succeeded/failed) rows. `FileDetailPage` (COL-34) already knows the exact
 * file path it wants history for, so it passes `filePath` to use the
 * server-side filter directly instead of fetching and filtering the whole
 * table.
 *
 * Uses a relative URL -- per `frontend/README.md`, the backend eventually
 * serves this bundle from its own origin, so no base URL is needed. Routed
 * through `apiFetch` (COL-33's `client.ts`) so the `X-Api-Key` header
 * (COL-26) rides along, same as every other `/api` call.
 */
export async function fetchJobHistory(filePath?: string): Promise<JobHistoryEntry[]> {
  const url = filePath ? `/api/jobs/history?file=${encodeURIComponent(filePath)}` : "/api/jobs/history";
  const response = await apiFetch(url);
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load job history (${response.status})`));
  }
  return (await response.json()) as JobHistoryEntry[];
}

/**
 * Fetches the live queue (`GET /api/jobs/queue`, COL-175): every currently
 * `running`/`pending` Job, ordered running-first then pending by ascending
 * `priority`. Used by `QueuePage` (COL-178) instead of {@link fetchJobHistory}
 * -- unlike that fetch-everything-and-filter-client-side approach, this
 * endpoint already returns exactly (and only) the rows the Queue view wants,
 * in the order it wants them displayed, so there's no client-side re-sort.
 */
export async function fetchJobQueue(): Promise<JobHistoryEntry[]> {
  const response = await apiFetch("/api/jobs/queue");
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to load queue (${response.status})`));
  }
  return (await response.json()) as JobHistoryEntry[];
}

/**
 * Manually enqueues a downmix job for one file (`POST /api/jobs/trigger`,
 * COL-29), used by `FileDetailPage`'s "Trigger downmix" action (COL-34).
 *
 * `extra_languages` is the allow-list-bypass option: languages listed there
 * are unioned onto the scheduler's `language_allow_list` for this one call,
 * letting a user downmix a language the global allow-list would otherwise
 * exclude. A `202` is returned whether or not a job was enqueued -- the
 * response's `enqueued` flag (not the HTTP status) distinguishes a queued
 * job from a skipped file, so this only throws on a genuine error response.
 */
export async function triggerDownmix(input: ManualTriggerRequest): Promise<ManualTriggerResult> {
  const response = await apiFetch("/api/jobs/trigger", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to trigger downmix (${response.status})`));
  }
  return (await response.json()) as ManualTriggerResult;
}

/**
 * Manually enqueues a `SET_DEFAULT_AUDIO` job for one file
 * (`POST /api/jobs/trigger-default-audio`, COL-155), used by
 * `FileDetailPage`'s "Set Default Audio Track" action (COL-157).
 *
 * Mirrors {@link triggerDownmix}'s request/response handling: a `202` is
 * returned whether or not a job was enqueued -- the response's `enqueued`
 * flag (not the HTTP status) distinguishes a queued job from a skipped
 * file, so this only throws on a genuine error response.
 */
export async function triggerSetDefaultAudio(
  input: SetDefaultAudioTriggerRequest,
): Promise<SetDefaultAudioTriggerResult> {
  const response = await apiFetch("/api/jobs/trigger-default-audio", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to trigger Set Default Audio Track (${response.status})`),
    );
  }
  return (await response.json()) as SetDefaultAudioTriggerResult;
}

/**
 * Manually enqueues `SET_DEFAULT_AUDIO` jobs for an arbitrary mixed-level
 * Library selection in one request (`POST /api/jobs/trigger-default-audio/bulk`,
 * COL-156), used by the Library page's bulk "Set Default Audio Track" action
 * (COL-158). Mirrors `bulkUpdateTracked`'s (`api/library.ts`, COL-103) shape:
 * `references` may hold any mix of Series/Season/Episode/Movie references,
 * cascaded and de-duplicated down to their unique on-disk files server-side.
 *
 * A `202` is returned whether or not any individual file was enqueued; each
 * result's own `enqueued` flag distinguishes queued from skipped, mirroring
 * {@link triggerSetDefaultAudio}'s single-file skip semantics per file.
 */
export async function bulkTriggerSetDefaultAudio(
  references: TrackedNodeReference[],
): Promise<BulkSetDefaultAudioTriggerResult> {
  const body: BulkSetDefaultAudioTriggerRequest = { references };
  const response = await apiFetch("/api/jobs/trigger-default-audio/bulk", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(
        response,
        `Failed to trigger bulk Set Default Audio Track (${response.status})`,
      ),
    );
  }
  return (await response.json()) as BulkSetDefaultAudioTriggerResult;
}

/**
 * Bumps one pending Job to the front of the queue (`POST /api/jobs/{job_id}/bump`,
 * COL-169) -- `QueuePage`'s (COL-180) per-row "Process next" action.
 *
 * A `200` is returned whether or not the bump actually took effect -- the
 * response's `bumped` flag (not the HTTP status) distinguishes success from
 * "too late" (the Job was no longer `pending` by the time the request
 * landed, e.g. a worker already claimed it), so this only throws on a
 * genuine error response (e.g. `404` when the job is no longer in the live
 * queue at all).
 */
export async function bumpJobToFront(jobId: string): Promise<BumpJobResult> {
  const response = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}/bump`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(
      await apiErrorMessage(response, `Failed to move job to the front of the queue (${response.status})`),
    );
  }
  return (await response.json()) as BumpJobResult;
}

/**
 * Cancels one pending Job (`DELETE /api/jobs/{job_id}`, COL-168) --
 * `QueuePage`'s (COL-180) per-row "Cancel" action. "Cancel" is deletion: on
 * success the Job is removed from the live queue and its `JobHistory` row
 * is deleted too, leaving no trace.
 *
 * A `200` is returned whether or not the cancel actually took effect -- the
 * response's `cancelled` flag (not the HTTP status) distinguishes success
 * from "too late" (a worker already claimed the Job, or it already reached
 * a terminal status, by the time the request landed), so this only throws
 * on a genuine error response (e.g. `404` when the job is no longer in the
 * live queue at all).
 */
export async function cancelJob(jobId: string): Promise<CancelJobResult> {
  const response = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to cancel job (${response.status})`));
  }
  return (await response.json()) as CancelJobResult;
}

/**
 * Requeues one specific file (`POST /api/jobs/requeue`, COL-170) --
 * `HistoryPage`'s (COL-176) per-row "Requeue" action on a `failed` row.
 *
 * A `202` is returned whether or not a job was actually enqueued -- the
 * response's `enqueued` flag (not the HTTP status) distinguishes a queued
 * job from a skipped file (already `PENDING`/`RUNNING`, unprobeable, or no
 * qualifying target), so this only throws on a genuine error response.
 * Unlike {@link cancelJob}/{@link bumpJobToFront}, the requeued file's
 * existing (terminal) `JobHistoryEntry` row is untouched -- the backend
 * creates a brand-new `Job`/history row for the requeue rather than mutating
 * the old one, so the failed row stays visible in `HistoryPage` exactly as
 * it was.
 */
export async function requeueFile(filePath: string): Promise<RequeueFileResult> {
  const body: RequeueFileRequest = { file_path: filePath };
  const response = await apiFetch("/api/jobs/requeue", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to requeue file (${response.status})`));
  }
  return (await response.json()) as RequeueFileResult;
}

/**
 * Cancels every currently-`pending` Job in one call (`POST /api/jobs/clear`,
 * COL-173) -- `QueuePage`'s (COL-181) page-level "Clear queue" action, unlike
 * {@link cancelJob}'s single-row scope.
 *
 * A `202` is always returned, even when the queue was already empty. Rather
 * than a single boolean/count, the response reports the `cancelled`/
 * `already_running` split so the caller can surface exactly what happened to
 * the pending-Job snapshot instead of a generic success message -- some of
 * that snapshot may have been claimed by a worker between the snapshot and
 * their own individual cancel, and that's reported, not swallowed.
 */
export async function clearQueue(): Promise<ClearQueueResult> {
  const response = await apiFetch("/api/jobs/clear", { method: "POST" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to clear queue (${response.status})`));
  }
  return (await response.json()) as ClearQueueResult;
}
