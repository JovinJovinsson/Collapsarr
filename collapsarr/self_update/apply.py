"""Pipx self-update apply flow: verify, upgrade, re-exec (COL-232).

Wires together COL-230's guard/checksum client, :mod:`collapsarr.
ffmpeg_download`'s injectable download-and-verify primitive, and this
repo's downmix-style injectable subprocess ``_Runner`` idiom
(:mod:`collapsarr.downmix.remux`/:mod:`collapsarr.downmix.probe`) into the
concrete "no Jobs running" pipx apply flow ``CONTEXT.md``'s **Self-Update**
entry describes: download and SHA-256-verify the target release's archive,
run ``pipx upgrade collapsarr`` as a subprocess, then re-exec the current
process into the freshly-installed code.

**Strictly sequential, never out of order.** :func:`apply_pipx_update`
verifies before it ever touches ``pipx upgrade`` -- a checksum mismatch (or
any other download failure) aborts the attempt and clears the in-progress
guard back to :data:`~collapsarr.self_update.models.PHASE_IDLE` *before*
``pipx upgrade`` would have run, never after. Both OS-touching steps
(``pipx upgrade`` and the re-exec) are injectable (``subprocess_runner``/
``reexec_fn``), so tests exercise this whole flow end to end with fakes/
spies -- never a real subprocess spawn or a real :func:`os.execv` call, per
this ticket's acceptance criteria.

**Scope.** :func:`apply_pipx_update` itself deliberately covers only the "no
Jobs running" case (COL-232's own ticket title) -- it never touches the Job
Queue. :func:`apply_with_flow` (COL-233) is the layer above it that does:
given an operator-selected **Cancel & Restart Now**/**Wait & Restart** flow
(``CONTEXT.md``'s **Self-Update** entry), it clears the Job Queue of
currently-``RUNNING`` Jobs -- either hard-killing and immediately requeuing
them, or simply waiting for them to finish naturally -- then calls straight
into :func:`apply_pipx_update` once none are left running. See
:func:`apply_with_flow`'s own docstring for the full contract, including how
it force-pauses (and, on any failure of its own, un-pauses) Auto-Processing
Pause around that wait.

**Guard handoff across the re-exec, not a clear.** Unlike every other
failure path here, a *successful* ``pipx upgrade`` deliberately leaves the
in-progress guard held (``in_progress`` stays ``True``) and only advances
``phase`` to :data:`~collapsarr.self_update.models.PHASE_AWAITING_HEALTH`
before re-exec'ing -- the freshly-exec'd process reads the very same
persisted row and continues the state machine from there. A later ticket's
health-check-within-timeout / auto-rollback
(``docs/adr/0009-self-update-staged-handoff-with-auto-rollback.md``) is what
eventually clears it back to idle (on success) or ``rolled_back`` (on
failure) -- this module never does that itself.

**No failure can leave the guard stuck.** Once :func:`apply_pipx_update`
has reserved the in-progress guard (:func:`~collapsarr.self_update.service.
begin_self_update`), everything through the successful hand-off above runs
inside a single ``try``/``except Exception`` block: every *expected* failure
(a checksum mismatch, a download error, a missing ``pipx`` executable, a
subprocess timeout, a non-zero ``pipx upgrade`` exit) raises the internal
:class:`_ApplyFailure`, and *any other, truly unexpected* exception (a
``PermissionError`` from a non-executable ``pipx``, a database error, ...)
is caught too -- both funnel through the exact same ``except`` clause, which
clears the guard back to :data:`~collapsarr.self_update.models.PHASE_IDLE`
before converting the failure into a :class:`SelfUpdateApplyOutcome`. This
is deliberate: the guard is a **persisted** row that survives a process
restart, and :func:`~collapsarr.self_update.service.begin_self_update`
refuses every future attempt while it is held -- so an exception that
skipped past an ad hoc ``clear_self_update`` call would poison every
subsequent self-update attempt indefinitely, with no way to recover short of
a manual DB edit.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from .. import __version__
from ..ffmpeg_download.client import download_and_verify
from ..jobs.queue import JobQueue, JobStatus
from ..jobs.scheduler import JobScheduler
from ..settings.models import UPDATE_CHANNEL_STABLE
from ..settings.service import restore_auto_processing_pause, stash_and_force_pause_processing
from ..update_check.client import GITHUB_REPO
from ..update_check.comparison import is_up_to_date
from ..update_check.models import UpdateCheckState
from .client import fetch_checksum_entry, resolve_platform_arch
from .models import (
    PHASE_APPLYING,
    PHASE_AWAITING_HEALTH,
    PHASE_DOWNLOADING,
    PHASE_IDLE,
    PHASE_PREPARING,
    PHASE_VERIFYING,
)
from .service import begin_self_update, clear_self_update, set_self_update_phase

logger = logging.getLogger(__name__)

#: Generous timeout for the full release archive download -- comparable to
#: :data:`collapsarr.ffmpeg_download.service.DOWNLOAD_TIMEOUT`'s budget for a
#: similarly-sized third-party archive.
DOWNLOAD_TIMEOUT = 180.0

#: Hard ceiling on the downloaded archive size, forwarded to
#: :func:`~collapsarr.ffmpeg_download.client.download_and_verify` as
#: ``max_bytes`` -- same defense-in-depth reasoning as
#: :data:`collapsarr.ffmpeg_download.service.MAX_FFMPEG_ARCHIVE_BYTES`: bound
#: this process's peak memory against a compromised/misbehaving redirect
#: target, independent of the SHA-256 check that runs once the full body has
#: already been read.
MAX_RELEASE_ARCHIVE_BYTES = 400 * 1024 * 1024

#: ``pipx upgrade`` itself is a local pip-resolver operation (PyPI index
#: fetch + install into the existing venv), not a large download -- a much
#: tighter budget than the archive download above.
PIPX_UPGRADE_TIMEOUT = 120.0

#: The exact command ``CONTEXT.md``'s **Self-Update** entry names for the
#: ``pipx`` apply flow -- matches this repo's package name
#: (``pyproject.toml``'s ``[project].name``).
PIPX_UPGRADE_COMMAND: tuple[str, ...] = ("pipx", "upgrade", "collapsarr")

#: Injectable subprocess seam, matching :mod:`collapsarr.downmix.remux`'s/
#: :mod:`collapsarr.downmix.probe`'s ``_Runner`` idiom exactly:
#: ``(command, timeout) -> subprocess.CompletedProcess``. Defaults to a thin
#: wrapper around :func:`subprocess.run` (:func:`_run_pipx_upgrade`); tests
#: inject a fake/spy so no real ``pipx`` subprocess is ever spawned.
SubprocessRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]

#: Injectable re-exec seam: ``() -> None``. Never returns in production
#: (:func:`_reexec_process`'s :func:`os.execv` call replaces the running
#: process image); a test fake/spy returns normally instead, so a test that
#: reaches this point doesn't have to survive a real process replacement.
ReexecFn = Callable[[], None]


class _ApplyFailure(RuntimeError):
    """Internal control-flow signal: an expected failure inside the guarded region.

    Raised for every one of :func:`apply_pipx_update`'s *expected* failure
    modes (checksum mismatch, download error, missing ``pipx``, subprocess
    timeout, non-zero ``pipx upgrade`` exit) so they funnel through the exact
    same ``except`` clause as any genuinely unexpected exception -- see the
    module docstring's "No failure can leave the guard stuck" section. Never
    escapes :func:`apply_pipx_update` itself; always caught and converted
    into a :class:`SelfUpdateApplyOutcome`.
    """


@dataclass(frozen=True, slots=True)
class SelfUpdateApplyOutcome:
    """Outcome of one :func:`apply_pipx_update` attempt.

    ``ok=False`` covers every expected failure mode -- unsupported platform/
    architecture, no checksum entry for this release, a download/checksum
    failure, or a non-zero ``pipx upgrade`` exit -- with ``error`` carrying a
    short human-readable reason the route layer passes straight through. On
    every ``ok=False`` outcome the in-progress guard has already been
    cleared back to :data:`~collapsarr.self_update.models.PHASE_IDLE` before
    this returns, and ``pipx upgrade`` was never invoked unless verification
    had already succeeded (see the module docstring).

    ``ok=True`` is only actually observed by a caller when ``reexec_fn``
    does not itself terminate the process (i.e. in tests, with a fake) -- a
    real re-exec never returns, so production never sees this value used.
    """

    ok: bool
    error: str | None = None


def stable_update_target(state: UpdateCheckState | None, running_version: str) -> str | None:
    """Return the stable release tag COL-232's apply flow should target, or ``None``.

    The single seam both :mod:`collapsarr.self_update.routes`' apply
    endpoint (its "is an update even available" gate) and
    :func:`apply_pipx_update`'s callers use, so they can never drift on what
    counts as an applicable release. Reuses the *persisted*
    :class:`~collapsarr.update_check.models.UpdateCheckState` row (COL-86)
    rather than fetching GitHub again -- per this ticket's own instructions.

    Deliberately narrower than :func:`collapsarr.update_check.service.
    is_update_available`: this only ever targets a **stable** release,
    regardless of the operator's configured **Release Channel**. If the
    persisted row's ``channel`` is ``beta`` (or, defensively, anything other
    than ``stable``), ``None`` is returned even if ``latest_tag`` itself
    looks newer -- a beta tag (e.g. ``beta-v0.2.1.0007``) is never a valid
    ``pipx upgrade collapsarr`` target, and comparing it against
    :func:`~collapsarr.update_check.comparison.is_up_to_date`'s stable-tag
    identity check would otherwise spuriously report "available" for every
    beta build. ``None`` before the first Update Check tick has ever run
    (``state`` is ``None``) or before one has ever resolved a tag
    (``state.latest_tag`` is ``None``), and ``None`` when the running
    version already matches the latest stable tag (nothing to apply).
    """
    if state is None or state.latest_tag is None:
        return None
    if state.channel != UPDATE_CHANNEL_STABLE:
        return None
    if is_up_to_date(running_version, state.latest_tag):
        return None
    return state.latest_tag


def _release_asset_url(tag: str, filename: str) -> str:
    """Build a GitHub Release asset download URL for ``tag``/``filename``.

    ``https://github.com/<owner>/<repo>/releases/download/<tag>/<filename>``
    -- GitHub's standard release-asset URL shape, matching how
    ``release.yml``'s own verification job downloads published assets back
    (``gh release download``, COL-227) and requiring no authentication for a
    public repo's public release assets.
    """
    return f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{filename}"


def _run_pipx_upgrade(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """The real, production :data:`SubprocessRunner` -- a thin wrapper around
    :func:`subprocess.run`, matching :mod:`collapsarr.downmix.remux`'s
    ``_run_ffmpeg``/:mod:`collapsarr.downmix.probe`'s ``_run_ffprobe``
    convention exactly."""
    return subprocess.run(  # noqa: S603 - command is the fixed PIPX_UPGRADE_COMMAND literal, not shell text
        list(command), capture_output=True, text=True, timeout=timeout, check=False
    )


def _reexec_process() -> None:
    """The real, production :data:`ReexecFn`: replace this process in place.

    ``os.execv(sys.argv[0], sys.argv)`` -- the standard "restart myself"
    idiom: ``sys.argv[0]`` is the absolute path to the console-script pipx
    installed (``~/.local/bin/collapsarr`` by pipx's own convention), and
    re-executing that exact path picks up the just-upgraded package's code
    since ``pipx upgrade`` already updated the venv it points into. Never
    returns on success -- the calling process image is replaced outright, so
    nothing after this call in :func:`apply_pipx_update` ever runs in
    production; only a test's fake/spy :data:`ReexecFn` returns normally.
    """
    os.execv(sys.argv[0], sys.argv)  # noqa: S606 - restarting this exact process, not an arbitrary command


def _run_guarded_apply(
    session: Session,
    *,
    target_tag: str,
    platform_name: str,
    arch: str,
    version: str,
    transport: httpx.BaseTransport | None,
    timeout: float,
    subprocess_timeout: float,
    run: SubprocessRunner,
    reexec: ReexecFn,
) -> SelfUpdateApplyOutcome:
    """The guarded body of the pipx apply flow -- assumes the guard is *already reserved*.

    Factored out of :func:`apply_pipx_update` (COL-232) so :func:`apply_with_flow`
    (COL-233) can reserve the in-progress guard itself, *before* its own
    cancel/wait phase, and then run this exact same verify/download/upgrade/
    re-exec sequence once Jobs are clear -- without a second, redundant (and
    would-be-rejected) :func:`~collapsarr.self_update.service.begin_self_update`
    call. Every caller of this function must have already reserved the guard;
    this function never reserves it, only clears it on failure (see below).

    Steps, in order: fetch + resolve the ``SHA256SUMS`` entry for
    ``target_tag`` (:func:`~collapsarr.self_update.client.fetch_checksum_entry`);
    download and SHA-256-verify the resolved archive
    (:func:`~collapsarr.ffmpeg_download.client.download_and_verify`, bounded
    by :data:`MAX_RELEASE_ARCHIVE_BYTES`) -- ``pipx upgrade`` is never reached
    on a network failure or checksum mismatch (the AC's "verify before
    upgrade, strictly sequential" requirement); run
    :data:`PIPX_UPGRADE_COMMAND` via ``run``; on success, advance ``phase`` to
    :data:`~collapsarr.self_update.models.PHASE_AWAITING_HEALTH` --
    **without** clearing ``in_progress`` (see the module docstring's "Guard
    handoff" section) -- and call ``reexec``.

    Every failure -- expected (a checksum mismatch, download error, missing
    ``pipx``, subprocess timeout, non-zero ``pipx upgrade`` exit, each raised
    internally as :class:`_ApplyFailure`) or not -- is funnelled through one
    ``except Exception`` clause that clears the guard back to idle; see the
    module docstring's "No failure can leave the guard stuck" section.
    """
    try:
        checksum_url = _release_asset_url(target_tag, "SHA256SUMS")
        checksum_result = fetch_checksum_entry(
            checksum_url, version, platform_name, arch, transport=transport, timeout=timeout
        )
        if (
            not checksum_result.ok
            or checksum_result.filename is None
            or checksum_result.sha256 is None
        ):
            raise _ApplyFailure(checksum_result.error or "Failed to resolve the release checksum.")

        archive_url = _release_asset_url(target_tag, checksum_result.filename)
        download_result = download_and_verify(
            archive_url,
            checksum_result.sha256,
            timeout=timeout,
            transport=transport,
            max_bytes=MAX_RELEASE_ARCHIVE_BYTES,
        )
        if not download_result.ok:
            # Network failure or a SHA-256 mismatch -- abort before pipx
            # upgrade is ever invoked (AC: verify-then-upgrade, strictly
            # sequential; the running install is untouched).
            raise _ApplyFailure(download_result.error or "Failed to download the release archive.")

        # Verification has now completed successfully -- advance the phase
        # marker before moving on to the actual upgrade step.
        set_self_update_phase(session, PHASE_VERIFYING)
        set_self_update_phase(session, PHASE_APPLYING)

        try:
            result = run(PIPX_UPGRADE_COMMAND, subprocess_timeout)
        except FileNotFoundError as exc:
            raise _ApplyFailure("pipx executable not found on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise _ApplyFailure(
                f"pipx upgrade collapsarr timed out after {subprocess_timeout}s."
            ) from exc

        if result.returncode != 0:
            raise _ApplyFailure(
                f"pipx upgrade collapsarr failed (exit {result.returncode}): {result.stderr}"
            )

        # pipx upgrade succeeded -- the new code is installed on disk. Leave
        # the guard held (in_progress stays True) and hand off to
        # awaiting_health; see the module docstring's "Guard handoff across
        # the re-exec" section.
        set_self_update_phase(session, PHASE_AWAITING_HEALTH)
        logger.info("Self-update to %s applied via pipx; re-executing.", target_tag)
        reexec()

        # Reached only when `reexec` is a test fake/spy that returns instead
        # of replacing the process -- a real os.execv() never returns on
        # success.
        return SelfUpdateApplyOutcome(ok=True)
    except Exception as exc:
        # Everything from here down is the single place every guarded-region
        # failure -- a deliberate _ApplyFailure raised above, or any other,
        # genuinely unexpected exception (a PermissionError from a
        # non-executable pipx binary, a DB commit error, ...) -- funnels
        # through. See the module docstring's "No failure can leave the
        # guard stuck" section for why this must be broad rather than
        # enumerating every possible exception type.
        clear_self_update(session, phase=PHASE_IDLE)
        if not isinstance(exc, _ApplyFailure):
            logger.exception(
                "Self-update apply to %s failed unexpectedly via pipx", target_tag
            )
        return SelfUpdateApplyOutcome(ok=False, error=str(exc) or repr(exc))


def apply_pipx_update(
    session: Session,
    *,
    target_tag: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
    subprocess_timeout: float = PIPX_UPGRADE_TIMEOUT,
    subprocess_runner: SubprocessRunner | None = None,
    reexec_fn: ReexecFn | None = None,
) -> SelfUpdateApplyOutcome:
    """Download, verify, ``pipx upgrade``, and re-exec into ``target_tag`` (COL-232).

    The core "no Jobs running" pipx apply flow -- see the module docstring
    for the full sequencing contract and scope. Steps, in order:

    1. Resolve this process's ``(platform, arch)``
       (:func:`~collapsarr.self_update.client.resolve_platform_arch``) --
       ``ok=False`` immediately, before the in-progress guard is even taken,
       if this platform/arch has no release archive.
    2. Reserve the single-flight guard
       (:func:`~collapsarr.self_update.service.begin_self_update`), stamping
       the running version as the rollback target. Raises
       :class:`~collapsarr.self_update.service.SelfUpdateAlreadyInProgressError`
       -- **not caught here** -- if an attempt is already in progress; the
       caller (:mod:`collapsarr.self_update.routes`) maps that to a ``409``.
    3. Runs :func:`_run_guarded_apply` -- fetch/verify the checksum, download
       + verify the archive, run ``pipx upgrade``, and (on success) re-exec.
       See that function's own docstring for the full step-by-step contract.

    Once the guard is reserved (step 2), every failure -- expected or not --
    is funneled through :func:`_run_guarded_apply`'s own ``except Exception``
    clause, which clears it back to idle; see the module docstring's "No
    failure can leave the guard stuck" section. ``transport`` is forwarded to
    every network call this makes (tests inject an ``httpx.MockTransport``;
    production leaves it ``None`` for a real network call), matching every
    other transport-injectable seam in this codebase.
    """
    run = subprocess_runner or _run_pipx_upgrade
    reexec = reexec_fn or _reexec_process

    resolved = resolve_platform_arch()
    if resolved is None:
        return SelfUpdateApplyOutcome(
            ok=False,
            error="Self-update is not available for this platform/architecture.",
        )
    platform_name, arch = resolved
    version = target_tag[1:] if target_tag.startswith("v") else target_tag

    # Reserve the single-flight guard before any network I/O -- a second
    # concurrent trigger must be rejected right here, never partway through
    # a download. Propagates SelfUpdateAlreadyInProgressError to the caller
    # rather than catching it: that guard is COL-230's, this function adds
    # no second one of its own. Deliberately outside the try/except below --
    # a failure to even take the guard must never attempt to clear it.
    begin_self_update(session, previous_version=__version__, phase=PHASE_DOWNLOADING)

    return _run_guarded_apply(
        session,
        target_tag=target_tag,
        platform_name=platform_name,
        arch=arch,
        version=version,
        transport=transport,
        timeout=timeout,
        subprocess_timeout=subprocess_timeout,
        run=run,
        reexec=reexec,
    )


# --------------------------------------------------------------------------- #
# In-flight Job handling: Cancel & Restart Now / Wait & Restart (COL-233)
# --------------------------------------------------------------------------- #

FLOW_CANCEL_AND_RESTART = "cancel_and_restart"
FLOW_WAIT_AND_RESTART = "wait_and_restart"

#: Every ``flow`` value :func:`apply_with_flow` accepts.
IN_FLIGHT_FLOWS = (FLOW_CANCEL_AND_RESTART, FLOW_WAIT_AND_RESTART)

#: "Wait & Restart"'s ceiling on how long the request blocks for currently-
#: ``RUNNING`` Jobs to finish naturally before giving up. A genuinely
#: unbounded wait inside one HTTP request is a real UX/timeout risk (a
#: reverse proxy or browser has its own ceiling far below "however long the
#: longest ffmpeg remux takes"), so this flow is bounded rather than passing
#: ``timeout=None`` through to :meth:`~collapsarr.jobs.queue.JobQueue.
#: wait_no_running` -- a operator whose Jobs are still running after ten
#: minutes gets a clear failure (guard cleared, pause restored, nothing left
#: paused) and can retry, rather than a request that never returns. Generous
#: enough to cover a normal-sized remux; a future ticket could move this
#: whole wait off the request-handling thread (background it, poll via the
#: status endpoint) if operators hit this ceiling in practice, but that is a
#: bigger structural change than this ticket's scope.
WAIT_AND_RESTART_TIMEOUT = 600.0

#: "Cancel & Restart Now"'s ceiling on how long it waits, after firing the
#: hard-kill signal at every ``RUNNING`` Job, for them to actually finish
#: transitioning to their terminal ``FAILED`` status (:meth:`~collapsarr.
#: jobs.queue.JobQueue.cancel_running` only *signals* the kill -- see its own
#: docstring -- the worker thread still has to observe the dead subprocess
#: and run the ordinary terminal path). Needed before requeuing: a Job whose
#: status hasn't yet flipped past ``RUNNING`` still reads as "active" to
#: :meth:`~collapsarr.jobs.scheduler.JobScheduler._is_duplicate`, so
#: requeuing too early would be silently dropped as a duplicate. Far shorter
#: than :data:`WAIT_AND_RESTART_TIMEOUT` -- killing a subprocess and letting
#: the worker observe it is fast; this is not "wait for the Job to finish its
#: work" like that constant is.
CANCEL_DRAIN_TIMEOUT = 30.0


def _cancel_and_restart_running_jobs(
    queue: JobQueue, scheduler: JobScheduler, *, drain_timeout: float
) -> None:
    """Cancel & Restart Now: hard-cancel every ``RUNNING`` Job, then requeue each (COL-233).

    Enumerates every currently-``RUNNING`` Job (a plain filter over
    :meth:`~collapsarr.jobs.queue.JobQueue.list_jobs` -- there is no
    dedicated "list running" primitive on :class:`JobQueue` worth adding for
    this one caller) and hard-kills each via :meth:`~collapsarr.jobs.queue.
    JobQueue.cancel_running` -- exactly the existing manual mid-flight cancel
    path (COL-192): the killed subprocess makes the pipeline call fail
    naturally, and the worker transitions the Job to ``FAILED`` through the
    ordinary terminal path, so no new terminal status is needed here (per
    this ticket's own AC).

    Waits (:meth:`~collapsarr.jobs.queue.JobQueue.wait_no_running`, bounded
    by ``drain_timeout``) for every one of those kills to actually land
    before requeuing anything -- a Job still reading as ``RUNNING`` is still
    "active" as far as :meth:`~collapsarr.jobs.scheduler.
    JobScheduler._is_duplicate` is concerned, so requeuing before the kill
    has actually taken effect would be silently swallowed as a duplicate
    trigger rather than genuinely restarting the file. Raises
    :class:`_ApplyFailure` if the drain itself times out -- funnelled by
    :func:`apply_with_flow`'s caller through the same broad-except path
    every other expected failure here uses.

    Once drained, requeues each cancelled Job's file
    (:meth:`~collapsarr.jobs.scheduler.JobScheduler.trigger_file`,
    ``bypass_dedup_window=True``) -- the same bypass the existing per-row
    Requeue action uses, so a hard-cancelled Job restarts immediately rather
    than being blocked by the Recently-Processed Window it would otherwise
    just have tripped (per this ticket's own AC). A file with nothing left
    to do, or one a concurrent enqueue/top-up already re-queued in the
    meantime, is silently skipped (``trigger_file`` returning ``None``) --
    the same "too late, not an error" shape every other trigger path in this
    codebase already has.
    """
    running = [job for job in queue.list_jobs() if job.status is JobStatus.RUNNING]
    for job in running:
        queue.cancel_running(job.id)
    if running and not queue.wait_no_running(timeout=drain_timeout):
        raise _ApplyFailure(
            f"Timed out after {drain_timeout}s waiting for hard-cancelled Jobs to "
            "stop running."
        )
    for job in running:
        scheduler.trigger_file(job.file_path, bypass_dedup_window=True)


def apply_with_flow(
    session: Session,
    *,
    target_tag: str,
    flow: str,
    queue: JobQueue,
    scheduler: JobScheduler | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
    subprocess_timeout: float = PIPX_UPGRADE_TIMEOUT,
    subprocess_runner: SubprocessRunner | None = None,
    reexec_fn: ReexecFn | None = None,
    wait_timeout: float = WAIT_AND_RESTART_TIMEOUT,
    cancel_drain_timeout: float = CANCEL_DRAIN_TIMEOUT,
) -> SelfUpdateApplyOutcome:
    """Handle in-flight Jobs per ``flow``, then apply the update (COL-233).

    The layer :mod:`collapsarr.self_update.routes`' apply endpoint calls
    when the operator has picked an explicit in-flight-Job flow (as opposed
    to the plain "no Jobs running" :func:`apply_pipx_update` path it still
    calls directly when there is nothing to handle). ``flow`` is
    :data:`FLOW_CANCEL_AND_RESTART` or :data:`FLOW_WAIT_AND_RESTART`
    (:data:`IN_FLIGHT_FLOWS`) -- anything else raises :class:`ValueError`.
    ``scheduler`` is required for :data:`FLOW_CANCEL_AND_RESTART` (it
    requeues through it); ``ValueError`` if omitted for that flow.

    Steps, in order:

    1. Resolve this process's ``(platform, arch)`` -- ``ok=False``
       immediately, before the in-progress guard is even taken, if this
       platform/arch has no release archive. Mirrors
       :func:`apply_pipx_update` step 1 exactly.
    2. Reserve the *real* single-flight guard
       (:func:`~collapsarr.self_update.service.begin_self_update`, phase
       :data:`~collapsarr.self_update.models.PHASE_PREPARING`) -- **before**
       Auto-Processing Pause or the Job Queue is ever touched, and held for
       the *entire* cancel/wait window that follows, not just the
       download/upgrade steps afterward. This closes a real race an earlier
       version of this function had: a bare "peek" at the guard's current
       state (read, then act) leaves a window -- up to ``wait_timeout``/
       ``cancel_drain_timeout`` wide -- during which a second concurrent
       trigger could pass the same peek, stash its own snapshot of
       ``auto_processing_paused`` (by then already forced ``True`` by the
       first attempt), and corrupt the restore value. Reserving the real
       guard here instead makes a second concurrent call fail immediately,
       with :class:`~collapsarr.self_update.service.SelfUpdateAlreadyInProgressError`
       propagating straight out -- **not caught here**, same contract as
       :func:`apply_pipx_update` -- before it ever stashes anything.
    3. Stashes the pre-update ``auto_processing_paused`` value and force-sets
       it ``True`` (:func:`~collapsarr.settings.service.
       stash_and_force_pause_processing`) -- both flows do this,
       unconditionally, per this ticket's AC. Safe now: the guard reserved
       in step 2 guarantees no concurrent attempt can be mid-flight here too.
    4. Runs the chosen flow: :data:`FLOW_CANCEL_AND_RESTART` hard-cancels and
       immediately requeues every ``RUNNING`` Job
       (:func:`_cancel_and_restart_running_jobs`); :data:`FLOW_WAIT_AND_RESTART`
       simply waits (:meth:`~collapsarr.jobs.queue.JobQueue.wait_no_running`,
       bounded by ``wait_timeout``) for them to finish naturally, cancelling
       nothing. Any failure here (including a wait/drain timeout) clears the
       guard back to idle *and* restores Auto-Processing Pause immediately
       (:func:`~collapsarr.settings.service.restore_auto_processing_pause`)
       -- this process is not about to restart, so there is no "next boot"
       to rely on -- and returns ``ok=False``.
    5. Once Jobs are clear, runs :func:`_run_guarded_apply` -- the same
       verify/download/upgrade/re-exec sequence :func:`apply_pipx_update`
       runs, reusing the guard already reserved in step 2 rather than taking
       a second one. ``ok=False`` (a checksum mismatch, download failure, or
       failed ``pipx upgrade`` -- the guard has already been cleared by
       :func:`_run_guarded_apply` itself) restores Auto-Processing Pause
       here too, since this process keeps running.
    6. On ``ok=True`` (pipx upgrade succeeded; re-exec is imminent -- or, in
       a test, a fake ``reexec_fn`` returned instead), Auto-Processing Pause
       is deliberately left forced and ``auto_processing_pause_restore_value``
       stays set -- there is no restart *in this process* to hang the
       restore off, so it is consumed at the next boot instead
       (:func:`~collapsarr.settings.service.restore_auto_processing_pause`,
       called early in :func:`collapsarr.main.create_app`'s ``lifespan``,
       before the Job Queue starts accepting work again).
    """
    if flow not in IN_FLIGHT_FLOWS:
        raise ValueError(f"Unknown self-update flow: {flow!r}; expected one of {IN_FLIGHT_FLOWS!r}")
    if flow == FLOW_CANCEL_AND_RESTART and scheduler is None:
        raise ValueError("`scheduler` is required for the 'cancel_and_restart' flow.")

    run = subprocess_runner or _run_pipx_upgrade
    reexec = reexec_fn or _reexec_process

    resolved = resolve_platform_arch()
    if resolved is None:
        return SelfUpdateApplyOutcome(
            ok=False,
            error="Self-update is not available for this platform/architecture.",
        )
    platform_name, arch = resolved
    version = target_tag[1:] if target_tag.startswith("v") else target_tag

    # Reserve the real guard *before* Auto-Processing Pause or the Job Queue
    # is touched at all, and hold it across the whole cancel/wait phase --
    # not just apply_pipx_update's download/upgrade steps. Propagates
    # SelfUpdateAlreadyInProgressError to the caller rather than catching it
    # -- deliberately outside the try/except below, same as
    # apply_pipx_update's own guard reservation: a failure to even take the
    # guard must never attempt to clear it or touch the pause.
    begin_self_update(session, previous_version=__version__, phase=PHASE_PREPARING)

    try:
        stash_and_force_pause_processing(session)
        if flow == FLOW_CANCEL_AND_RESTART:
            assert scheduler is not None  # checked above
            _cancel_and_restart_running_jobs(queue, scheduler, drain_timeout=cancel_drain_timeout)
        else:
            if not queue.wait_no_running(timeout=wait_timeout):
                raise _ApplyFailure(
                    f"Timed out after {wait_timeout}s waiting for running Jobs to finish."
                )
    except Exception as exc:
        clear_self_update(session, phase=PHASE_IDLE)
        restore_auto_processing_pause(session)
        if not isinstance(exc, _ApplyFailure):
            logger.exception(
                "Self-update in-flight Job handling failed unexpectedly (flow=%s)", flow
            )
        return SelfUpdateApplyOutcome(ok=False, error=str(exc) or repr(exc))

    outcome = _run_guarded_apply(
        session,
        target_tag=target_tag,
        platform_name=platform_name,
        arch=arch,
        version=version,
        transport=transport,
        timeout=timeout,
        subprocess_timeout=subprocess_timeout,
        run=run,
        reexec=reexec,
    )
    if not outcome.ok:
        restore_auto_processing_pause(session)
    return outcome


__all__ = [
    "CANCEL_DRAIN_TIMEOUT",
    "DOWNLOAD_TIMEOUT",
    "FLOW_CANCEL_AND_RESTART",
    "FLOW_WAIT_AND_RESTART",
    "IN_FLIGHT_FLOWS",
    "MAX_RELEASE_ARCHIVE_BYTES",
    "PIPX_UPGRADE_COMMAND",
    "PIPX_UPGRADE_TIMEOUT",
    "WAIT_AND_RESTART_TIMEOUT",
    "ReexecFn",
    "SelfUpdateApplyOutcome",
    "SubprocessRunner",
    "apply_pipx_update",
    "apply_with_flow",
    "stable_update_target",
]
