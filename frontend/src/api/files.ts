import type { AudioStreamsResponse } from "../types/audioStreams";
import type { WantedFile } from "../types/wanted";
import { apiErrorMessage, apiFetch } from "./client";

/**
 * Thrown by {@link fetchFileById} on a genuine `404` -- a file id that was
 * never valid or no longer exists -- so callers (`FileDetailPage`) can tell
 * that apart from a transient/network failure and render the distinct
 * not-found state rather than a generic error message.
 */
export class FileNotFoundError extends Error {}

/**
 * Fetches a single tracked file by id (`GET /api/files/:id`, COL-203),
 * independent of whether it currently appears in `GET /api/wanted` -- unlike
 * `fetchWantedList` (`api/wanted.ts`), this resolves a fully-processed file
 * (zero missing targets) too, which is exactly why `FileDetailPage` reads
 * from here instead of scanning the wanted list for a matching id.
 *
 * Uses a relative URL -- per `frontend/README.md`, the backend eventually
 * serves this bundle from its own origin, so no base URL is needed. Routed
 * through `apiFetch` (COL-33's `client.ts`) so the `X-Api-Key` header
 * (COL-26) rides along, same as every other `/api` call.
 */
export async function fetchFileById(fileId: string | number): Promise<WantedFile> {
  const response = await apiFetch(`/api/files/${encodeURIComponent(String(fileId))}`);
  if (!response.ok) {
    const message = await apiErrorMessage(response, `Failed to load file (${response.status})`);
    if (response.status === 404) {
      throw new FileNotFoundError(message);
    }
    throw new Error(message);
  }
  return (await response.json()) as WantedFile;
}

/**
 * Fetches a single file's *current, live-probed* audio streams
 * (`GET /api/files/:id/audio-streams`, COL-204) -- re-probed by the backend
 * on every call (never cached/stored), so this always reflects the file's
 * actual state on disk right now, including which stream currently carries
 * the Default Audio Track disposition.
 *
 * Unlike {@link fetchFileById}, an unprobeable file (missing on disk,
 * corrupt) is still a `200` with `probeable: false` -- only a genuinely
 * unknown `file_id` throws {@link FileNotFoundError} here, matching
 * `fetchFileById`'s 404 handling.
 */
export async function fetchAudioStreams(fileId: string | number): Promise<AudioStreamsResponse> {
  const response = await apiFetch(
    `/api/files/${encodeURIComponent(String(fileId))}/audio-streams`,
  );
  if (!response.ok) {
    const message = await apiErrorMessage(
      response,
      `Failed to load audio streams (${response.status})`,
    );
    if (response.status === 404) {
      throw new FileNotFoundError(message);
    }
    throw new Error(message);
  }
  return (await response.json()) as AudioStreamsResponse;
}

/**
 * Poster metadata for a file (`GET /api/files/:id/poster`, COL-205).
 *
 * No Plex integration exists yet, so the backend always responds with
 * `status: "placeholder"` / `poster_url: null` for a file that exists --
 * this is a deliberately stable contract Phase 2 (COL-212) satisfies by
 * populating `poster_url` and flipping `status` to `"available"`, without
 * this shape (or how callers read it) needing to change.
 */
export interface FilePosterResponse {
  file_id: number;
  status: "available" | "placeholder";
  poster_url: string | null;
}

/**
 * Fetches poster metadata for a file (`GET /api/files/:id/poster`, COL-205).
 *
 * A poster is decorative, never load-bearing: `poster_url: null` is the
 * normal (current-phase) response, not a failure, and any rejection here
 * (network error, non-2xx, a malformed body) should be treated identically
 * by callers -- fall back to the placeholder graphic, never surface an
 * error state for a missing poster.
 */
export async function fetchFilePoster(fileId: string | number): Promise<FilePosterResponse> {
  const response = await apiFetch(`/api/files/${encodeURIComponent(String(fileId))}/poster`);
  if (!response.ok) {
    const message = await apiErrorMessage(response, `Failed to load poster (${response.status})`);
    throw new Error(message);
  }
  return (await response.json()) as FilePosterResponse;
}
