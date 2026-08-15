/**
 * Small formatting helpers shared across pages that render raw
 * bytes/timestamps from the API. Extracted from `BackupsPage.tsx`'s
 * original `formatSize` (COL-63) when `StatusPage.tsx` (COL-123) needed the
 * identical byte-formatting logic for disk usage -- rather than a second,
 * independent copy of the same 1024-step unit walk.
 */

/**
 * Formats a byte count as a human-readable size, e.g. `"1.4 MB"`, `"500 B"`.
 *
 * `NaN`/negative input (an unexpected/unset value) renders as an em dash
 * rather than a nonsensical size.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}
