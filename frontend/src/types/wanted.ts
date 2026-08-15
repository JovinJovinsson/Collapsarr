/**
 * Types mirroring the `/api/wanted` response (COL-28,
 * `collapsarr/media/routes.py`): `WantedFile`/`WantedTarget` there are
 * Pydantic models built from `collapsarr.downmix.targets.DownmixTarget` and
 * `collapsarr.media.models.TrackedMediaFile` -- kept in sync by hand since
 * there's no shared schema generation yet.
 */

/** Matches `collapsarr.downmix.targets.DownmixTarget`'s enum values. */
export type DownmixTarget = "stereo" | "2.1" | "5.1";

/**
 * Shared value/label pairs for every `DownmixTarget` picker in Settings
 * (Targets' enabled-targets toggles, Preferred Default Audio's channel-tier
 * picker) -- one source of truth for the display label per target.
 */
export const DOWNMIX_TARGET_OPTIONS: { value: DownmixTarget; label: string }[] = [
  { value: "stereo", label: "Stereo (2.0)" },
  { value: "2.1", label: "2.1" },
  { value: "5.1", label: "5.1" },
];

/** One `(language, target)` pair still missing on a wanted file. */
export interface WantedTarget {
  language: string;
  target: DownmixTarget;
}

/**
 * A tracked file missing at least one enabled downmix target.
 *
 * `library_node_id`/`node_type`/`tracked` (COL-101) are the bridge to this
 * file's owning `LibraryNode` (`collapsarr.library`) and its resolved
 * **Tracked** value -- see `collapsarr/media/routes.py`'s module docstring.
 * All three are `null` together when the bridge hasn't resolved (no
 * instance/episode/movie id captured yet, e.g. a file only ever manually
 * triggered by bare path) -- `FileDetailPage` shows a "status unavailable"
 * message rather than a broken toggle in that case. `library_node_id` is
 * exactly the `node_id` a Tracked toggle passes to `updateTracked`
 * (`api/library.ts`), and `node_type` is always `"episode"` or `"movie"`
 * when set (a Series/Season never owns a tracked-media file directly).
 */
export interface WantedFile {
  id: number;
  file_path: string;
  missing_targets: WantedTarget[];
  created_at: string;
  updated_at: string;
  library_node_id: number | null;
  node_type: "episode" | "movie" | null;
  tracked: boolean | null;
}
