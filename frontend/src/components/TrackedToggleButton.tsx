/**
 * The Tracked/Not-Tracked toggle button (COL-101): reads a Library node's
 * *resolved* Tracked value and flips it via `POST /api/library/tracked`
 * (`api/library.ts#updateTracked`) through the caller's `onToggle`. Shared by
 * `LibraryPage` (one per Series/Season/Episode/Movie row) and
 * `FileDetailPage` (one for the file's bridged node) so the same look and
 * pending/disabled behaviour reads as one control across both surfaces.
 *
 * `pending` (this toggle's own request in flight) disables the button and
 * swaps its label so a double-click can't fire two overlapping requests. The
 * button never optimistically flips ahead of the server: a Series/Season
 * toggle cascades to descendants the button itself has no knowledge of, so
 * the caller re-fetches and passes back the server's resulting `tracked`
 * value rather than this component guessing it.
 */
export function TrackedToggleButton({
  tracked,
  pending,
  onToggle,
}: {
  tracked: boolean;
  pending: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={
        tracked ? "tracked-toggle tracked-toggle--on" : "tracked-toggle tracked-toggle--off"
      }
      onClick={onToggle}
      disabled={pending}
      aria-pressed={tracked}
    >
      {pending ? "Updating…" : tracked ? "Tracked" : "Not Tracked"}
    </button>
  );
}
