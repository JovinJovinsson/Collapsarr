/**
 * Types mirroring the `/api/settings` response (COL-28,
 * `collapsarr/settings/routes.py`) -- kept in sync by hand since there's no
 * shared schema generation yet.
 */

import type { DownmixTarget } from "./wanted";

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
}
