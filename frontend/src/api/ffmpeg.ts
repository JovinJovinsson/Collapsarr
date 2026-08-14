import { apiErrorMessage, apiFetch } from "./client";

/** Response shape for a successful `POST /api/system/ffmpeg/download`. */
export interface FfmpegDownloadResult {
  ffmpeg_path: string;
}

/**
 * Triggers the opt-in FFmpeg auto-download
 * (`POST /api/system/ffmpeg/download`, COL-222): downloads the pinned,
 * checksum-verified build for this platform (COL-217), extracts it into
 * `<data_dir>/ffmpeg/`, and persists the resolved path onto
 * `GlobalSettings.ffmpeg_path` (COL-218). Never triggered automatically --
 * this is only ever called from the health banner's opt-in "Download
 * FFmpeg" action (`HealthBanner.tsx`), which is itself hidden for Docker
 * installs (the server also 403s a Docker install that calls this
 * directly).
 *
 * Throws with a clear, server-provided message on any failure (offline,
 * source unreachable, checksum mismatch, extraction failure, or the Docker
 * gate) -- the server never persists a partial/unverified path on failure,
 * so the caller can simply retry by calling this again.
 */
export async function downloadFfmpeg(): Promise<FfmpegDownloadResult> {
  const response = await apiFetch("/api/system/ffmpeg/download", { method: "POST" });
  if (!response.ok) {
    throw new Error(await apiErrorMessage(response, `Failed to download FFmpeg (${response.status})`));
  }
  return (await response.json()) as FfmpegDownloadResult;
}
