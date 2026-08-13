/**
 * Types mirroring the `/api/plex/connection` response (COL-209,
 * `collapsarr/plex/routes.py`) -- kept in sync by hand since there's no
 * shared schema generation yet.
 */

/** Matches `collapsarr.plex.models.ConnectivityStatus`'s enum values. */
export type PlexConnectivityStatus = "unknown" | "ok" | "error";

/**
 * The singleton Plex connection row, decoded to its JSON response shape.
 * Unlike `ArrInstance` (`types/instances.ts`), there is no `token` field --
 * the Plex token never reaches the browser (COL-209's acceptance criteria).
 * `has_token` lets the UI show "a token is configured" without ever reading
 * the secret itself.
 */
export interface PlexConnection {
  base_url: string;
  has_token: boolean;
  status: PlexConnectivityStatus;
  status_error: string | null;
  status_checked_at: string | null;
  version: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Request body for `PUT /api/plex/connection`. Both fields are optional --
 * only fields present are changed (`collapsarr.plex.routes.
 * PlexConnectionUpdate`'s partial-update convention). Omitting `token` on an
 * edit leaves the currently-stored token untouched -- since a `GET` never
 * echoes it back, the form has no way to round-trip it, so it should only be
 * sent when the operator types a new one.
 */
export interface PlexConnectionUpdateInput {
  base_url?: string;
  token?: string;
}
