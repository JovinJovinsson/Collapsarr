/**
 * Types mirroring the `/api/wanted` response (COL-28,
 * `collapsarr/media/routes.py`): `WantedFile`/`WantedTarget` there are
 * Pydantic models built from `collapsarr.downmix.targets.DownmixTarget` and
 * `collapsarr.media.models.TrackedMediaFile` -- kept in sync by hand since
 * there's no shared schema generation yet.
 */

/** Matches `collapsarr.downmix.targets.DownmixTarget`'s enum values. */
export type DownmixTarget = "stereo" | "2.1" | "5.1";

/** One `(language, target)` pair still missing on a wanted file. */
export interface WantedTarget {
  language: string;
  target: DownmixTarget;
}

/** A tracked file missing at least one enabled downmix target. */
export interface WantedFile {
  id: number;
  file_path: string;
  missing_targets: WantedTarget[];
  created_at: string;
  updated_at: string;
}
