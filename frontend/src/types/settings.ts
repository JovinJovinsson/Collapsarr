/**
 * Types mirroring the `/api/settings` response (COL-28,
 * `collapsarr/settings/routes.py`) -- kept in sync by hand since there's no
 * shared schema generation yet.
 */

import type { DownmixTarget } from "./wanted";

/**
 * The required-mode for the UI/session auth gate (COL-51,
 * `collapsarr.settings.models.AUTH_REQUIRED_ENABLED` /
 * `AUTH_REQUIRED_LOCAL_BYPASS`): `"local_bypass"` (the default) skips the
 * auth challenge for a caller whose direct connection is loopback or a
 * private-range address; `"enabled"` always challenges, regardless of
 * address -- the right choice when Collapsarr sits behind a reverse proxy,
 * since classification never looks past the proxy's own peer address.
 */
export type AuthRequiredMode = "enabled" | "local_bypass";

/**
 * How the UI credential is presented (COL-52,
 * `collapsarr.settings.models.AUTH_METHOD_FORMS` / `AUTH_METHOD_BASIC`):
 * `"forms"` (the default) is the sign-in page; `"basic"` is the browser's
 * native HTTP Basic prompt, verifying the same stored credential. "Remember
 * me" is a Forms-only concept -- Basic re-sends credentials every request.
 */
export type AuthMethod = "forms" | "basic";

/**
 * Which GitHub Release stream the Update Check compares the running instance
 * against (COL-88, `collapsarr.settings.models.UPDATE_CHANNEL_STABLE` /
 * `UPDATE_CHANNEL_BETA`): `"stable"` (the default) is the latest non-prerelease
 * Release; `"beta"` is the latest prerelease Release.
 */
export type UpdateChannel = "stable" | "beta";

/**
 * Runtime override for the `collapsarr` logger (COL-130,
 * `collapsarr.settings.models.LOG_LEVELS`). `null` means "no override, fall
 * back to the `COLLAPSARR_LOG_LEVEL` environment setting at boot" (default
 * `"INFO"`); a concrete value takes effect immediately, without a restart.
 */
export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR";

/** The persisted global settings row, decoded to its JSON response shape. */
export interface GlobalSettings {
  enabled_targets: DownmixTarget[];
  language_allow_list: string[] | null;
  stereo_codec: string;
  stereo_bitrate_kbps: number | null;
  surround_codec: string;
  surround_bitrate_kbps: number | null;
  concurrency_limit: number;
  ui_auth_enabled: boolean;
  auth_required: AuthRequiredMode;
  auth_method: AuthMethod;
  /** Days between scheduled backups (COL-66). Default 7; positive integer. */
  backup_interval_days: number;
  /** Days a backup is kept before pruning (COL-66). Default 28; positive integer. */
  backup_retention_days: number;
  /**
   * Free-space percentage below which the disk-space health check warns
   * (`WARN-DISK-001`, COL-79). Default 5; read live on every check tick.
   */
  disk_space_warning_percent: number;
  /**
   * Free-space percentage below which the disk-space health check escalates
   * to an error (`ERR-DISK-001`, COL-79). Default 2; read live on every
   * check tick.
   */
  disk_space_error_percent: number;
  /** Which GitHub Release stream the Update Check compares against (COL-88). Default `"stable"`. */
  update_channel: UpdateChannel;
  /**
   * Default Tracked setting for newly-discovered library items (COL-98).
   * When a Library node has no explicit ancestor override, its Tracked value
   * resolves to this instance-wide default. Default `true`.
   */
  default_tracked: boolean;
  /**
   * Runtime log-level override for the `collapsarr` logger (COL-130).
   * `null` when unset -- the level is then whatever `COLLAPSARR_LOG_LEVEL`
   * resolved to at boot.
   */
  log_level: LogLevel | null;
  /**
   * Preferred Default Audio language code (COL-151), e.g. `"eng"`. `null`
   * when unset -- paired with `default_audio_channel_tier` to pick which
   * resulting downmix track becomes a file's default audio track.
   */
  default_audio_language: string | null;
  /**
   * Preferred Default Audio channel tier (COL-151). `null` when unset --
   * paired with `default_audio_language`.
   */
  default_audio_channel_tier: DownmixTarget | null;
  /**
   * Opt-in gate (COL-151) for whether the downmix pipeline actually applies
   * the Preferred Default Audio preference. Default `false`; only takes
   * effect once both `default_audio_language` and
   * `default_audio_channel_tier` are set.
   */
  auto_set_default_audio: boolean;
  /**
   * Recently-processed window for the scheduler's dedup cooldown (COL-167).
   * In minutes; `0` disables the cooldown entirely (always eligible for
   * retry). Default 360 (6 hours). Read live on every dedup check -- no
   * restart needed after saving.
   */
  recently_processed_window_minutes: number;
  /**
   * Auto-Queuing Pause (COL-174). When `true`, the scanner's Wanted-driven
   * auto-fill (the periodic scan's initial enqueue and the Auto-Queue
   * Limit's completion/cancellation-triggered top-up) stops running;
   * already-`pending`/`running` Jobs and every manual trigger are
   * unaffected. `QueuePage`'s (COL-181) page-level toggle reads/writes this
   * field. Default `false`.
   */
  auto_queue_paused: boolean;
  /**
   * Auto-Processing Pause (COL-226). When `true`, the Job Queue's worker
   * pool stops claiming any new `pending` Job; already-`running` Jobs
   * finish normally. Deliberately distinct from `auto_queue_paused`: that
   * field only stops the scanner from *adding* new pending Jobs, while this
   * one stops already-pending Jobs (however they got there) from ever
   * starting. `QueuePage`'s page-level toggle reads/writes this field.
   * Default `false`.
   */
  auto_processing_paused: boolean;
  /** Auto-generated, read-only -- never set through this body. */
  api_key: string;
  created_at: string;
  updated_at: string;
}

/**
 * Request body for `PUT /api/settings`. Every field is optional -- only
 * fields present in the request are changed (`collapsarr.settings.routes.
 * SettingsUpdate`'s partial-update convention). Sending an explicit `null`
 * for `language_allow_list`/`stereo_bitrate_kbps`/`surround_bitrate_kbps`
 * clears the stored override; omitting the field leaves it untouched.
 */
export interface GlobalSettingsUpdateInput {
  enabled_targets?: DownmixTarget[];
  language_allow_list?: string[] | null;
  stereo_codec?: string;
  stereo_bitrate_kbps?: number | null;
  surround_codec?: string;
  surround_bitrate_kbps?: number | null;
  concurrency_limit?: number;
  ui_auth_enabled?: boolean;
  auth_required?: AuthRequiredMode;
  auth_method?: AuthMethod;
  backup_interval_days?: number;
  backup_retention_days?: number;
  disk_space_warning_percent?: number;
  disk_space_error_percent?: number;
  update_channel?: UpdateChannel;
  default_tracked?: boolean;
  log_level?: LogLevel | null;
  default_audio_language?: string | null;
  default_audio_channel_tier?: DownmixTarget | null;
  auto_set_default_audio?: boolean;
  recently_processed_window_minutes?: number;
  auto_queue_paused?: boolean;
  auto_processing_paused?: boolean;
}
