/** Backup type partition on disk (COL-63). Only `manual` is produced today. */
export type BackupType = "manual" | "scheduled" | "update";

/** One backup archive, as returned by `GET /api/system/backup`. */
export interface Backup {
  /** `<type>/<filename>`, unambiguous across the type subdirectories. */
  id: string;
  name: string;
  type: BackupType;
  /** Archive size in bytes. */
  size: number;
  /** ISO-8601 UTC creation timestamp; displayed in local time. */
  created_at: string;
}

/**
 * `GET /api/system/backup` response. `supported` is `false` when the database
 * isn't file-based SQLite -- the page shows its "unavailable" state instead of
 * the backup controls.
 */
export interface BackupList {
  supported: boolean;
  backups: Backup[];
}
