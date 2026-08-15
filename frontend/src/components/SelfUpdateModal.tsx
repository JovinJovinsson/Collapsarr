import type { InstallMethod } from "../types/system";
import type { SelfUpdateFlow } from "../types/updates";
import { Modal } from "./Modal";

/**
 * The "Update Now" confirmation modal (COL-228, `CONTEXT.md`'s
 * **Self-Update** entry). Built on `Modal` (a real dialog) rather than this
 * codebase's more common inline `.view__confirm` panel (`QueuePage`'s
 * "Clear queue"/"Process Now", `BackupsPage`, `HistoryPage`) -- a deliberate
 * choice: the ticket's own acceptance criteria call for "a confirmation
 * modal" specifically, and this action (unlike those page-level/per-row
 * ones) can hand off into a multi-step apply flow worth its own focused
 * dialog rather than another inline panel competing for space on the page.
 *
 * `UpdatesPage` resolves `runningJobCount` (a `GET /api/jobs/queue`
 * running-count) *before* opening this modal, so it always already knows
 * which of three shapes to render:
 *
 * - `runningJobCount > 0` and `installMethod === "native"` -- **blocked**:
 *   the apply endpoint (`collapsarr/self_update/routes.py`) 409s any
 *   non-`null` `flow` on a `native` install (COL-233's in-flight-Job
 *   handling only wraps the `pipx` path so far, see
 *   `SelfUpdateApplyRequest`'s doc comment in `types/updates.ts`) --
 *   offering the two flow choices here would just be two guaranteed-`409`
 *   buttons, so this shows an explanatory message and only a close action
 *   instead.
 * - `runningJobCount > 0` and `installMethod !== "native"` (i.e. `pipx`) --
 *   the job-count-aware branch: shows the count and the two in-flight-Job
 *   flow choices, **Cancel & Restart Now** (`"cancel_and_restart"`) and
 *   **Wait & Restart** (`"wait_and_restart"`).
 * - `runningJobCount === 0` -- a plain confirm; `onConfirm` is called with
 *   no `flow` (the apply endpoint's `flow` is optional when nothing is
 *   running).
 *
 * Declining (the backdrop, Escape, the close button, or the explicit
 * "Cancel"/"Close" button -- all routed through `Modal`'s single `onClose`)
 * never calls `onConfirm`, leaving the app untouched, per this ticket's
 * acceptance criteria. `onClose` is a no-op while `applying` is `true` --
 * `UpdatesPage` already disables every button for the same reason, so this
 * only guards the backdrop-click/Escape paths from closing out from under an
 * in-flight request.
 */
export function SelfUpdateModal({
  runningJobCount,
  installMethod,
  applying,
  error,
  onConfirm,
  onClose,
}: {
  runningJobCount: number;
  installMethod: InstallMethod;
  applying: boolean;
  error: string | null;
  onConfirm: (flow?: SelfUpdateFlow) => void;
  onClose: () => void;
}) {
  const hasRunningJobs = runningJobCount > 0;
  const jobCountText = `${runningJobCount} job${runningJobCount === 1 ? "" : "s"} ${
    runningJobCount === 1 ? "is" : "are"
  } currently running`;
  // See the module doc comment: native's staged-handoff apply flow doesn't
  // support the in-flight-Job flow parameter yet, so with Jobs running it's
  // a hard block rather than a choice.
  const nativeFlowUnsupported = hasRunningJobs && installMethod === "native";

  function handleClose(): void {
    if (applying) return;
    onClose();
  }

  return (
    <Modal title="Update Collapsarr now?" onClose={handleClose}>
      {nativeFlowUnsupported ? (
        <>
          <p>
            {jobCountText}. In-flight Job handling is not supported yet for native installs -- wait
            until they finish, then try again.
          </p>
          <div className="form-actions">
            <button type="button" className="btn btn--ghost" onClick={handleClose}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <p>
            {hasRunningJobs
              ? `${jobCountText}. Choose how to proceed:`
              : "This downloads, verifies, and installs the latest stable release, then restarts Collapsarr."}
          </p>
          {error && <p className="view__error">{error}</p>}
          <div className="form-actions">
            {hasRunningJobs ? (
              <>
                <button
                  type="button"
                  className="btn btn--danger"
                  onClick={() => onConfirm("cancel_and_restart")}
                  disabled={applying}
                >
                  {applying ? "Updating…" : "Cancel & Restart Now"}
                </button>
                <button
                  type="button"
                  className="btn btn--secondary"
                  onClick={() => onConfirm("wait_and_restart")}
                  disabled={applying}
                >
                  {applying ? "Updating…" : "Wait & Restart"}
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => onConfirm(undefined)}
                disabled={applying}
              >
                {applying ? "Updating…" : "Update Now"}
              </button>
            )}
            <button type="button" className="btn btn--ghost" onClick={handleClose} disabled={applying}>
              Cancel
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}
