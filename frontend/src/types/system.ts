/**
 * Types mirroring the About-panel system-info endpoint
 * (`collapsarr/system/info.py`) -- kept in sync by hand since there's no
 * shared schema generation yet.
 */

/** Raw free/total byte counts for the filesystem backing `data_dir`. */
export interface DiskUsageInfo {
  free_bytes: number;
  total_bytes: number;
}

/** How this instance is running (`CONTEXT.md`'s "Install Method"), matching `collapsarr/system/info.py::InstallMethod`. */
export type InstallMethod = "docker" | "pipx";

/**
 * The About panel's environment/runtime facts, as returned by
 * `GET /api/system/info` (COL-123,
 * `collapsarr/system/info.py::SystemInfoRead`).
 */
export interface SystemInfo {
  app_version: string;
  install_method: InstallMethod;
  python_version: string;
  /** `null` when FFmpeg isn't installed/resolvable -- not an error state here. */
  ffmpeg_version: string | null;
  os: string;
  /** The actually-configured SQLAlchemy dialect, e.g. `"sqlite"`, `"postgresql"`. */
  db_engine: string;
  /** The database's live applied Alembic revision; `null` only for an unmigrated DB. */
  db_schema_revision: string | null;
  data_dir: string;
  database_path: string;
  uptime_seconds: number;
  timezone: string;
  disk: DiskUsageInfo;
}
