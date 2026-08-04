/**
 * Backup type partition on disk. `manual` (COL-63) is the "Backup Now" type,
 * `scheduled` (COL-67) the scheduler's, `update` (COL-69) the pre-migration
 * safety copy, and `restore` (COL-70) the pre-restore safety copy the boot-time
 * swap engine takes before applying a restore.
 */
export type BackupType = "manual" | "scheduled" | "update" | "restore";

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
