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
}
