import { useEffect, useState } from "react";

import { fetchSelfUpdateStatus } from "../api/updates";
import { selfUpdatePhaseCopy } from "../utils/selfUpdatePhaseCopy";

/** How often to re-poll `GET /api/system/self-update/status` (COL-231). */
const POLL_INTERVAL_MS = 2_000;

type TerminalState = { previousVersion: string | null };

/**
 * The dedicated "Updating — please wait" screen (COL-231). `UpdatesPage`
 * renders this in place of its normal panel content from the moment
 * confirming `SelfUpdateModal` either succeeds or the connection drops
 * (see that page's `handleConfirmSelfUpdate` -- a real apply re-execs/exits
 * the server process, so a dropped connection right after calling it is the
 * *expected* production shape of success, not a failure worth surfacing as
 * one; see `applySelfUpdate`'s doc comment).
 *
 * Polls `GET /api/system/self-update/status` (`fetchSelfUpdateStatus`) every
 * {@link POLL_INTERVAL_MS} for as long as this component is mounted,
 * following the same self-scheduling `setTimeout`-loop-with-a-`cancelled`-
 * flag shape `QueuePage` uses for its own live poll. A poll that fails
 * outright (a network error, thrown by `fetch` itself rather than a
 * responded error status) is the *expected* shape of the app's downtime
 * window while the process re-execs/exits/relaunches -- this keeps showing
 * the last phase it successfully read (`reachable` only gates a small
 * "still restarting" footnote, never a hard error state) and keeps polling
 * rather than surfacing a dead end.
 *
 * A successful read settles into one of three shapes:
 *
 * - `phase === "rolled_back"` -- the post-re-exec health gate
 *   (`collapsarr/self_update/health_gate.py`) rolled back to
 *   `previous_version`. Polling stops; renders the terminal
 *   "Update failed — rolled back to vX.Y.Z" state instead of reloading, per
 *   this ticket's AC.
 * - `phase === "idle" && !in_progress` -- the update completed and its
 *   health check passed (`idle` is also the pre-attempt default, but by the
 *   time this component is ever mounted an attempt is already under way --
 *   see `handleConfirmSelfUpdate` -- so reading `idle` here always means
 *   "finished", never "never started"). Polling stops; the page reloads
 *   (`window.location.reload()`) to pick up the new build.
 * - anything else -- still in progress; the phase-to-copy mapping
 *   (`selfUpdatePhaseCopy`) updates and polling continues.
 */
export function SelfUpdateProgress({ runningJobCount }: { runningJobCount: number }) {
  const [phase, setPhase] = useState<string | null>(null);
  const [reachable, setReachable] = useState(true);
  const [terminal, setTerminal] = useState<TerminalState | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      let status;
      try {
        status = await fetchSelfUpdateStatus();
      } catch {
        if (cancelled) return;
        setReachable(false);
        timeoutId = setTimeout(() => void poll(), POLL_INTERVAL_MS);
        return;
      }
      if (cancelled) return;
      setReachable(true);

      if (status.phase === "rolled_back") {
        setTerminal({ previousVersion: status.previous_version });
        return; // Terminal failure state -- stop polling.
      }
      if (status.phase === "idle" && !status.in_progress) {
        window.location.reload();
        return; // Success -- the reload replaces this screen.
      }
      setPhase(status.phase);
      timeoutId = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    }

    void poll();

    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, []);

  if (terminal) {
    return (
      <div className="panel self-update-progress self-update-progress--failed">
        <h2 className="self-update-progress__title">Update failed</h2>
        <p className="self-update-progress__message" role="alert">
          Update failed — rolled back to{" "}
          {terminal.previousVersion ? `v${terminal.previousVersion}` : "the previous version"}.
        </p>
      </div>
    );
  }

  return (
    <div className="panel self-update-progress" role="status">
      <span className="self-update-progress__spinner" aria-hidden />
      <h2 className="self-update-progress__title">Updating — please wait</h2>
      <p className="self-update-progress__message">
        {selfUpdatePhaseCopy(phase ?? "preparing", runningJobCount)}
      </p>
      {!reachable && (
        <p className="self-update-progress__footnote">
          Collapsarr is restarting — this page will keep checking until it&apos;s back.
        </p>
      )}
    </div>
  );
}
