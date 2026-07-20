/**
 * Types mirroring the `/health` response (COL-38, `collapsarr/main.py`) --
 * kept in sync by hand since there's no shared schema generation yet.
 */

/** One failed startup check (currently just FFmpeg availability). */
export interface HealthWarning {
  code: string;
  message: string;
}

/** The `/health` liveness probe's response shape. */
export interface HealthStatus {
  status: "ok" | "degraded";
  version: string;
  warnings: HealthWarning[];
}
