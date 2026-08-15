/**
 * Maps a `SelfUpdateStatus.phase` (`types/updates.ts`) to the copy
 * `SelfUpdateProgress` (COL-231) shows while an attempt is still under way
 * -- this ticket's AC: "Waiting for N jobs to finish" -> "Downloading
 * update" -> "Restarting". Lives in its own module (rather than inline in
 * `components/SelfUpdateProgress.tsx`) purely so that file keeps exporting
 * only the component -- mixing a component export with a plain function
 * export in the same file trips `react-refresh/only-export-components`,
 * same reasoning `components/WantedFileRedirect.tsx`'s doc comment gives for
 * its own split from `routes/router.tsx`.
 *
 * `runningJobCount` is only meaningful for `"preparing"` -- it's the count
 * `UpdatesPage` resolved *before* opening `SelfUpdateModal` (COL-228), fixed
 * for the life of the polling screen since the phase itself doesn't report
 * a live count. An unrecognized `phase` (a newer backend's vocabulary this
 * build hasn't caught up with yet -- `types/updates.ts`'s `SelfUpdatePhase`
 * doc comment) falls through to a generic default rather than a type error.
 */
export function selfUpdatePhaseCopy(phase: string, runningJobCount: number): string {
  switch (phase) {
    case "preparing":
      return `Waiting for ${runningJobCount} job${runningJobCount === 1 ? "" : "s"} to finish…`;
    case "downloading":
      return "Downloading update…";
    case "verifying":
      return "Verifying update…";
    case "applying":
      return "Installing update…";
    case "awaiting_health":
      return "Restarting…";
    case "idle":
      return "Finishing up…";
    default:
      return "Updating…";
  }
}
