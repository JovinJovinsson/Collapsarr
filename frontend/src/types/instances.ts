/**
 * Types mirroring the `/api/instances` and
 * `/api/instances/{id}/path-mappings` responses (COL-27,
 * `collapsarr/arr/routes.py`) -- kept in sync by hand since there's no
 * shared schema generation yet.
 */

/** Matches `collapsarr.arr.models.InstanceType`'s enum values. */
export type InstanceType = "sonarr" | "radarr";

/** Matches `collapsarr.arr.models.ConnectivityStatus`'s enum values. */
export type ConnectivityStatus = "unknown" | "ok" | "error";

/** A configured Sonarr/Radarr connection, including cached connectivity state. */
export interface ArrInstance {
  id: number;
  name: string;
  type: InstanceType;
  base_url: string;
  api_key: string;
  status: ConnectivityStatus;
  status_error: string | null;
  status_checked_at: string | null;
  version: string | null;
  created_at: string;
  updated_at: string;
}

/** Request body for `POST /api/instances`. */
export interface InstanceCreateInput {
  name: string;
  type: InstanceType;
  base_url: string;
  api_key: string;
}

/**
 * Request body for `PUT /api/instances/{id}`. `type` is intentionally
 * absent -- the backend doesn't allow switching an instance's type in
 * place, matching `collapsarr.arr.routes.InstanceUpdate`.
 */
export interface InstanceUpdateInput {
  name?: string;
  base_url?: string;
  api_key?: string;
}

/** A remote-to-local path prefix mapping belonging to one instance. */
export interface PathMapping {
  id: number;
  instance_id: number;
  remote_prefix: string;
  local_prefix: string;
  order: number;
  created_at: string;
  updated_at: string;
}

/** Request body for `POST /api/instances/{id}/path-mappings`. */
export interface PathMappingCreateInput {
  remote_prefix: string;
  local_prefix: string;
  order?: number;
}

/** Request body for `PUT /api/instances/{id}/path-mappings/{mappingId}`. */
export interface PathMappingUpdateInput {
  remote_prefix?: string;
  local_prefix?: string;
  order?: number;
}
