"""Native (PyInstaller) self-update: staged handoff + atomic swap (COL-235).

The ``native`` sibling of :mod:`collapsarr.self_update.apply`'s pipx flow. A
frozen/onedir build cannot ``pipx upgrade`` itself -- there is no venv and no
package index entry to upgrade *from*; the running code **is** a directory of
files (the executable, ``_internal/``, bundled ``static``/``migrations``) laid
down by PyInstaller's ``COLLECT`` step. Updating it therefore means replacing
that whole directory on disk, which the *running* process cannot safely do to
*itself* (on Windows its own executable is memory-mapped and locked; on POSIX
overwriting the files a running image was loaded from is a foot-gun). So the
update is split across **two processes**:

1. The **live** process (:func:`apply_native_update`) downloads and
   SHA-256-verifies the target release into a *staging directory beside* the
   live install dir (never into it), extracts the new install tree there, then
   **spawns a second, detached "finish-update" process from the staged tree**
   and exits -- but *only after* that handoff process has been launched.
2. The **handoff** process (:func:`finish_native_update`, entered via the
   :data:`FINISH_UPDATE_FLAG` argv contract that :mod:`collapsarr.__main__`
   dispatches) **waits for the live process's PID to fully exit**, performs the
   **atomic directory swap** (old install renamed aside, staged tree renamed
   into place), and **re-execs itself from the now-swapped-in install dir** so
   the freshly-installed code comes up serving normally.

**Why the PID wait is the load-bearing invariant.** The handoff process must
never touch the install directory while the old process is still alive: on
Windows the old executable holds a lock on its own image (the ``os.replace``
would fail); on POSIX a concurrent swap races the old process's own file
access. :func:`finish_native_update` therefore blocks on
:func:`wait_for_pid_exit` *before* the first rename and, if the old PID has not
gone within the timeout, **aborts without swapping at all** -- leaving the live
install completely untouched -- rather than swapping under a still-running
process. The wait reuses the same injectable, structurally-typed process
abstraction idiom the downmix hard-cancel path uses
(:mod:`collapsarr.downmix.cancellation`'s :class:`~collapsarr.downmix.
cancellation.KillableProcess` ``Protocol``): here the primitive we need is a
*liveness poll* of a bare PID (the handoff process only knows the old process's
integer PID, not a :class:`subprocess.Popen` handle -- the old process is its
*parent*, which exits and is reaped by init/PID 1, not by us), so
:data:`PidProbe` is a one-method structural seam in the same spirit, injected
by every test so no real ``os.kill`` ever runs.

**Why the swap is two renames, and exactly what is observable at each step.**
:func:`swap_install_dir` is deliberately *not* a single "replace directory"
call, because no such atomic primitive exists cross-platform for a *non-empty*
destination directory (``os.replace`` is atomic for files on both POSIX and
Windows, and atomic for a directory only when the destination does **not**
already exist). So the swap is two individually-atomic renames, each targeting
a **non-existent** destination:

* ``os.replace(live_dir, backup_dir)`` -- move the old install aside. Atomic.
  Observable states: either ``live_dir`` is the complete old install
  (pre-rename) or ``backup_dir`` is the complete old install and ``live_dir``
  does not exist (post-rename). Never a half-populated ``live_dir``.
* ``os.replace(staged_dir, live_dir)`` -- move the verified staged tree into
  place. Atomic. Observable states: either ``live_dir`` is absent and
  ``staged_dir`` is the complete new install (pre-rename) or ``live_dir`` is
  the complete new install (post-rename). Never a half-populated ``live_dir``.

The two renames are not a single transaction, so there is a **window** between
them in which ``live_dir`` does not exist and the old install lives at
``backup_dir``. This is the *only* mixed on-disk state, and it is fully
**recoverable**: a crash in that window leaves ``backup_dir`` holding the
complete, untouched old install and ``staged_dir`` holding the complete new
install -- nothing is lost or half-written. :func:`restore_from_backup` is the
recovery primitive (rename ``backup_dir`` back to ``live_dir``) a later
startup-recovery ticket calls; :func:`swap_install_dir` itself already performs
that same rollback inline if the *second* rename fails for a reason it can
observe (an :class:`OSError`), so the only unrecovered case is a hard process
death (SIGKILL/power loss) precisely inside the microsecond window between two
back-to-back renames.

**Cross-platform reach and its documented limits.** The two-non-existent-
destination-renames design is what keeps each step atomic on **both** POSIX and
Windows (Windows cannot atomically *replace* an existing directory, but can
rename a directory onto a non-existent name). Two real limits remain, and are
called out rather than hidden:

* **Same-filesystem requirement.** ``os.replace`` is only atomic (indeed only a
  rename rather than a copy) when source and destination are on the same
  filesystem. :func:`apply_native_update` therefore stages *beside* the install
  dir (:data:`NATIVE_STAGING_DIRNAME`/:data:`NATIVE_BACKUP_DIRNAME` under the
  install dir's parent), never under ``$TMPDIR``, which may be a different
  mount.
* **Windows self-lock.** The handoff process is *running from* the staged tree
  at the moment it renames that tree into place; on Windows a running
  executable's image is locked, so moving the directory it lives in can raise a
  sharing violation. This happy-path ticket implements and tests the mechanism
  with ``tmp_path`` directories (no locked images); making it robust against
  the Windows self-lock (e.g. copy-then-swap, or a tiny bootstrapper that is
  not itself inside the swapped tree) is a documented follow-up, not silently
  assumed to already work.

**Database and configuration are untouched by construction.** Everything this
module renames lives under the *install* directory
(:func:`resolve_install_dir`). The SQLite database and all configuration live
under the OS user-data directory (:attr:`collapsarr.config.Settings.data_dir`,
e.g. ``~/.local/share/collapsarr``), a completely separate path the swap never
names. A test asserts a DB/config file placed outside the install dir survives
the swap byte-for-byte.

**No external process supervisor.** The old-PID-wait + self-relaunch chain is
self-sufficient: nothing here assumes systemd/launchd will restart anything.
The handoff process is spawned detached (its own session/process group) so it
outlives the live process that spawned it, and it re-execs itself once the swap
is done. The whole flow is exercised directly in tests, never via a service
manager.

**Guard discipline mirrors the pipx flow exactly.** Once
:func:`apply_native_update` reserves the in-progress guard
(:func:`~collapsarr.self_update.service.begin_self_update`), every subsequent
failure -- expected (:class:`_NativeApplyFailure`) or genuinely unexpected --
funnels through one broad ``except Exception`` that clears the guard back to
:data:`~collapsarr.self_update.models.PHASE_IDLE`, so no failure can leave the
persisted guard stuck (see :mod:`collapsarr.self_update.apply`'s module
docstring for the full rationale). On the *success* path the guard is
deliberately left **held** (``in_progress`` stays ``True``, ``phase`` advances
to :data:`~collapsarr.self_update.models.PHASE_AWAITING_HEALTH`) and handed off
across the process boundary: the re-exec'd install reads the very same
persisted row and a later health-check/rollback ticket clears it.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from .. import __version__
from ..archive_safety import is_unsafe_archive_entry_path
from ..ffmpeg_download.client import download_and_verify
from .apply import (
    DOWNLOAD_TIMEOUT,
    MAX_RELEASE_ARCHIVE_BYTES,
    SelfUpdateApplyOutcome,
    _release_asset_url,
)
from .client import fetch_checksum_entry, resolve_platform_arch
from .models import (
    PHASE_APPLYING,
    PHASE_AWAITING_HEALTH,
    PHASE_DOWNLOADING,
    PHASE_IDLE,
    PHASE_VERIFYING,
)
from .service import begin_self_update, clear_self_update, set_self_update_phase

logger = logging.getLogger(__name__)

#: argv flag that puts a freshly-spawned staged process into "finish-update"
#: mode -- :mod:`collapsarr.__main__` dispatches on it (via
#: :func:`parse_finish_update_argv`) *before* it would otherwise start the
#: server. A distinctive, prefixed long option so it can never collide with a
#: real server argument.
FINISH_UPDATE_FLAG = "--finish-self-update"

#: Sibling directories of the live install dir (under its *parent*, so they
#: share the install dir's filesystem -- see the module docstring's
#: same-filesystem note) that :func:`apply_native_update` stages the new
#: install into and :func:`finish_native_update` renames the old install aside
#: to. Dot-prefixed and app-named so they are unobtrusive and unambiguous next
#: to the real install directory.
NATIVE_STAGING_DIRNAME = ".collapsarr-self-update-staging"
NATIVE_BACKUP_DIRNAME = ".collapsarr-self-update-backup"

#: The third sibling directory (COL-236): where the *new, unhealthy* build
#: swapped-back out of ``live_dir`` is quarantined for the instant it takes
#: to delete it. :func:`~collapsarr.self_update.health_gate.
#: resolve_awaiting_health`'s native rollback reuses :func:`swap_install_dir`
#: with the old/new roles reversed (``live_dir`` <-> :data:`NATIVE_BACKUP_DIRNAME`)
#: -- that function's contract requires its own ``backup_dir`` argument to be a
#: non-existent destination distinct from both of its other two arguments, so
#: the failed new build can't be swapped directly into
#: :data:`NATIVE_BACKUP_DIRNAME` (that name is about to hold the *old*,
#: proven-healthy build the AC requires survive). The AC only requires the old
#: build survive until proven healthy, not that a failed new build be kept
#: around, so this directory is deleted again immediately after the swap-back
#: commits -- it is never left on disk for an operator to find.
NATIVE_QUARANTINE_DIRNAME = ".collapsarr-self-update-quarantine"

#: How long the handoff process waits for the old PID to exit before giving up
#: and aborting the swap (see :func:`finish_native_update`). Generous: a clean
#: process exit is near-instant, but a live process finishing an in-flight
#: request (or, on Windows, releasing its executable's lock) can take a beat.
PID_WAIT_TIMEOUT = 30.0

#: Poll cadence for :func:`wait_for_pid_exit` -- short enough that the swap
#: starts promptly once the old process is gone, long enough not to spin a
#: core. Mirrors the "poll a liveness flag on a small interval" cadence rather
#: than any busy-loop.
PID_POLL_INTERVAL = 0.1


@dataclass(frozen=True, slots=True)
class FinishUpdateArgs:
    """The four facts the handoff process needs to complete the swap + re-exec.

    Serialised onto the spawned process's argv by
    :func:`build_finish_update_argv` and parsed back by
    :func:`parse_finish_update_argv`. ``old_pid`` is the live process's PID the
    handoff waits to exit; ``live_dir`` is the install directory to swap into
    place and re-exec from; ``staged_dir`` is the verified new install tree to
    move into ``live_dir``; ``backup_dir`` is the (non-existent) name the old
    install is renamed aside to.
    """

    old_pid: int
    live_dir: Path
    staged_dir: Path
    backup_dir: Path


@dataclass(frozen=True, slots=True)
class NativeFinishOutcome:
    """Outcome of one :func:`finish_native_update` attempt.

    ``ok=False`` covers the two happy-path-adjacent failure modes the handoff
    can hit without ever leaving mixed state on disk: the old PID not exiting
    within the wait timeout (the swap is skipped entirely, live install
    untouched) or the atomic swap raising an :class:`OSError` (already rolled
    back inline; live install restored). ``ok=True`` is only ever observed by a
    test whose ``reexec_fn`` returns instead of replacing the process image --
    a real re-exec never returns.
    """

    ok: bool
    error: str | None = None


class NativeExtractionError(RuntimeError):
    """The downloaded, checksum-verified release archive couldn't be extracted.

    Raised by :func:`_extract_archive` for a malformed archive or one carrying
    an unsafe (zip-slip/tar-slip) entry path. Mirrors
    :class:`collapsarr.ffmpeg_download.service.FfmpegExtractionError`. Always
    caught inside :func:`apply_native_update` and converted into a
    :class:`_NativeApplyFailure`; never propagates.
    """


class _NativeApplyFailure(RuntimeError):
    """Internal control-flow signal: an expected failure inside the guarded region.

    The native counterpart of :class:`collapsarr.self_update.apply._ApplyFailure`
    -- raised for every *expected* :func:`apply_native_update` failure (missing
    checksum entry, download/checksum failure, extraction failure) so they
    funnel through the exact same broad ``except`` clause as any genuinely
    unexpected exception, keeping the guard-clearing discipline identical to
    the pipx flow. Never escapes :func:`apply_native_update`.
    """


# --- injectable seams --------------------------------------------------------

#: Liveness poll of a bare PID: ``True`` while the process is still running,
#: ``False`` once it is gone. The native analogue of
#: :mod:`collapsarr.downmix.cancellation`'s :class:`~collapsarr.downmix.
#: cancellation.KillableProcess` ``Protocol`` -- a *structural*, injectable
#: seam (defaults to :func:`_pid_is_running`; every test injects a fake) so no
#: real ``os.kill`` is ever issued in a test. A poll rather than a kill because
#: the handoff process only wants to *observe* the old process exit, never
#: terminate it.
PidProbe = Callable[[int], bool]

#: ``time.sleep``-shaped seam, injected in tests so the poll loop doesn't
#: actually block wall-clock time.
Sleep = Callable[[float], None]

#: ``time.monotonic``-shaped seam, injected in tests to drive the wait
#: deadline deterministically.
Monotonic = Callable[[], float]

#: Spawns the detached handoff process from the staged tree. Defaults to
#: :func:`_spawn_handoff`; tests inject a spy so no real process is launched.
#: ``(args, staged_dir) -> None``.
HandoffSpawner = Callable[["FinishUpdateArgs", Path], None]

#: Terminates the live process *after* the handoff has been launched. Defaults
#: to :func:`_exit_process` (never returns); a test spy records the call and
#: returns, so the test can assert ordering without the process dying.
ExitFn = Callable[[], None]

#: Performs the atomic directory swap. Defaults to :func:`swap_install_dir`;
#: injectable so :func:`finish_native_update` tests can spy/stub it.
#: ``(live_dir, staged_dir, backup_dir) -> None``.
SwapFn = Callable[[Path, Path, Path], None]

#: Re-execs the handoff process from the swapped-in install dir. Defaults to
#: :func:`_reexec_into` (never returns); a test spy records the call.
#: ``(live_dir) -> None``.
ReexecFn = Callable[[Path], None]


# --- install-dir resolution --------------------------------------------------


def resolve_install_dir() -> Path:
    """Return the swappable install directory for this frozen (native) build.

    For a PyInstaller ``--onedir`` build (see ``packaging/pyinstaller/
    collapsarr.spec``), :data:`sys.executable` is the bundled ``collapsarr``
    binary *inside* the ``COLLECT`` output directory, so that directory -- the
    parent of the executable, holding the binary, ``_internal/``, and the
    bundled ``static``/``migrations`` data -- is the complete install tree the
    swap replaces. ``.resolve()`` first so a symlinked launcher resolves to the
    real bundle directory rather than the symlink's own parent.
    """
    return Path(sys.executable).resolve().parent


# --- process liveness + wait -------------------------------------------------


def _pid_is_running(pid: int) -> bool:
    """Return whether process ``pid`` is currently running. The default :data:`PidProbe`.

    POSIX: ``os.kill(pid, 0)`` sends no signal but performs the same
    permission/existence check a real signal would -- it raises
    :class:`ProcessLookupError` once the process is gone (and reaped), and
    :class:`PermissionError` if the process exists but is owned by another user
    (still "running" for our purposes). Deliberately **not** used on Windows,
    where Python's ``os.kill`` for a non-``CTRL_*`` signal calls
    ``TerminateProcess`` -- i.e. it would *kill* the process rather than probe
    it. On Windows we instead open a handle and query its exit code, treating
    "can't open / no such process" as gone. Any unexpected error is treated as
    "still running" so the caller keeps waiting (and eventually times out)
    rather than swapping under a process whose state we couldn't confirm.
    """
    if sys.platform == "win32":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown error probing the PID -- err on the side of "still running"
        # so we never swap the install dir out from under a process we can't
        # confirm has exited.
        return True
    return True


def _pid_is_running_windows(pid: int) -> bool:  # pragma: no cover - Windows-only
    """Windows liveness probe: open the process and read its exit code.

    ``OpenProcess`` returning NULL means the process is gone (or inaccessible,
    which for a process we just spawned as our own parent means gone).
    ``GetExitCodeProcess`` returning ``STILL_ACTIVE`` (259) means it is still
    running; any other exit code means it has terminated. Best-effort: any
    ctypes/WinAPI hiccup is treated as "still running" so the wait times out
    safely rather than swapping prematurely (see :func:`_pid_is_running`).
    """
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def wait_for_pid_exit(
    pid: int,
    *,
    timeout: float,
    poll_interval: float = PID_POLL_INTERVAL,
    is_running: PidProbe = _pid_is_running,
    sleep: Sleep = time.sleep,
    monotonic: Monotonic = time.monotonic,
) -> bool:
    """Block until process ``pid`` has exited, or ``timeout`` elapses.

    Returns ``True`` if the process exited within ``timeout`` (including the
    case where it was already gone on the first poll), ``False`` on timeout.
    The deadline is measured against :data:`Monotonic` (never wall-clock) so an
    NTP/DST clock jump can't shorten or extend the wait. ``is_running``/
    ``sleep``/``monotonic`` are injectable (see :data:`PidProbe` etc.) so tests
    drive this deterministically without a real process or real sleeping.

    This is the load-bearing gate the handoff process holds *before* it touches
    the install directory -- see the module docstring: a ``False`` return means
    the caller must **not** swap.
    """
    deadline = monotonic() + timeout
    while is_running(pid):
        if monotonic() >= deadline:
            return False
        sleep(poll_interval)
    return True


# --- atomic directory swap ---------------------------------------------------


def swap_install_dir(live_dir: Path, staged_dir: Path, backup_dir: Path) -> None:
    """Atomically replace ``live_dir``'s contents with ``staged_dir``'s.

    Two individually-atomic renames, each onto a **non-existent** destination
    (the only shape that is atomic for a directory on *both* POSIX and Windows
    -- see the module docstring):

    1. ``os.replace(live_dir, backup_dir)`` -- old install aside.
    2. ``os.replace(staged_dir, live_dir)`` -- staged install into place.

    Preconditions (the caller -- :func:`apply_native_update` -- guarantees
    these): ``live_dir`` and ``staged_dir`` both exist and are on the same
    filesystem, and ``backup_dir`` does **not** exist. Between the two renames
    ``live_dir`` transiently does not exist; that window is the only mixed
    state and is recoverable (see :func:`restore_from_backup`). If the *second*
    rename raises an :class:`OSError`, this function rolls the first one back
    inline (renames ``backup_dir`` back to ``live_dir``) before re-raising, so
    an observable failure never leaves ``live_dir`` missing -- only a hard
    process death precisely between the two renames can, and that is what
    :func:`restore_from_backup` exists to repair on the next start.
    """
    os.replace(live_dir, backup_dir)
    # From here until the next rename completes, ``live_dir`` does not exist and
    # the complete old install is at ``backup_dir``. Keep this window as small
    # as possible: nothing but the second rename happens between them.
    try:
        os.replace(staged_dir, live_dir)
    except OSError:
        # The staged tree couldn't be moved into place. Undo the first rename
        # so we don't leave the live path empty -- the old install goes back
        # exactly where it was. Then re-raise for the caller to report.
        os.replace(backup_dir, live_dir)
        raise


def restore_from_backup(live_dir: Path, backup_dir: Path) -> bool:
    """Repair a swap interrupted by a hard process death mid-rename. Returns whether it acted.

    The recovery primitive for the one unrecoverable-inline case in
    :func:`swap_install_dir`: a SIGKILL/power-loss precisely in the window
    between the two renames, which leaves ``live_dir`` missing and the complete
    old install stranded at ``backup_dir``. If exactly that state is observed
    (``live_dir`` absent, ``backup_dir`` present), the old install is renamed
    back into place and ``True`` is returned. Otherwise this is a no-op
    returning ``False`` -- if ``live_dir`` already exists the swap either never
    started or fully completed, and ``backup_dir`` is just a stale artifact to
    reclaim separately, never something to move over a live install. A later
    startup-recovery ticket calls this before the server binds; it is provided
    and tested here so the "recoverable" half of the swap's correctness
    argument is verifiable, not merely asserted.
    """
    if not live_dir.exists() and backup_dir.exists():
        os.replace(backup_dir, live_dir)
        return True
    return False


# --- archive extraction ------------------------------------------------------


def _extract_archive(content: bytes, source_url: str, dest_dir: Path) -> None:
    """Extract the whole release archive tree into ``dest_dir`` (a fresh staging dir).

    Unlike :mod:`collapsarr.ffmpeg_download.service` (which pulls a single
    named member), a native release archive *is* the install tree, so every
    entry is extracted. Still hardened with the shared zip-slip/tar-slip guard
    (:func:`collapsarr.archive_safety.is_unsafe_archive_entry_path`): every
    entry is validated up front and the archive is rejected outright
    (:class:`NativeExtractionError`) if *any* entry is unsafe, rather than
    skipped -- same "refuse a hostile archive, don't quietly drop its bad
    parts" posture as that module. Dispatches on the URL suffix
    (``.zip`` for Windows, ``.tar.*`` otherwise), matching
    :func:`collapsarr.ffmpeg_download.service._extract_ffmpeg_binary`. The tar
    path preserves file modes (executables stay executable) and applies
    Python's ``data`` extraction filter as belt-and-braces on top of the
    explicit pre-check.
    """
    import tarfile
    import zipfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    if source_url.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                for info in archive.infolist():
                    if is_unsafe_archive_entry_path(info.filename):
                        raise NativeExtractionError(
                            "The downloaded release archive contains an unsafe entry "
                            f"path ({info.filename!r}); it looks like a path-traversal "
                            "(zip-slip) attempt and was rejected."
                        )
                archive.extractall(dest_dir)  # noqa: S202 - every entry pre-validated above
        except zipfile.BadZipFile as exc:
            raise NativeExtractionError(
                "The downloaded release archive is not a valid zip file."
            ) from exc
        return

    try:
        with tarfile.open(fileobj=BytesIO(content), mode="r:*") as archive:
            for member in archive.getmembers():
                if is_unsafe_archive_entry_path(member.name):
                    raise NativeExtractionError(
                        "The downloaded release archive contains an unsafe entry "
                        f"path ({member.name!r}); it looks like a path-traversal "
                        "(tar-slip) attempt and was rejected."
                    )
            # Every member validated above; the data filter is defence-in-depth.
            archive.extractall(dest_dir, filter="data")  # noqa: S202 - pre-validated
    except tarfile.TarError as exc:
        raise NativeExtractionError(
            "The downloaded release archive is not a valid tar archive."
        ) from exc


def _resolve_staged_root(extraction_dir: Path) -> Path:
    """Resolve the staged *install root* inside a freshly-extracted archive.

    Release archives produced by ``release.yml``'s ``native-build-*`` jobs wrap
    the install tree in a single top-level directory (PyInstaller's ``COLLECT``
    ``name="collapsarr"`` -> a ``collapsarr/`` directory), so an extraction
    yields ``extraction_dir/collapsarr/...`` rather than the install files at
    the extraction root. When ``extraction_dir`` contains exactly one entry and
    it is a directory, that single directory is the staged root; otherwise
    (a flat archive whose files sit directly at the root) ``extraction_dir``
    itself is. Both cases stay on the same filesystem as the install dir, so
    the subsequent :func:`swap_install_dir` rename remains atomic either way.
    """
    entries = list(extraction_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extraction_dir


# --- argv contract for the handoff process -----------------------------------


def build_finish_update_argv(executable: str, args: FinishUpdateArgs) -> list[str]:
    """Build the argv that launches ``executable`` in finish-update mode.

    The inverse of :func:`parse_finish_update_argv`. ``executable`` is the
    *staged* binary (the handoff must run the new code, not the old), and the
    four :class:`FinishUpdateArgs` fields are serialised as ``--flag value``
    pairs behind :data:`FINISH_UPDATE_FLAG`.
    """
    return [
        executable,
        FINISH_UPDATE_FLAG,
        "--old-pid",
        str(args.old_pid),
        "--live-dir",
        str(args.live_dir),
        "--staged-dir",
        str(args.staged_dir),
        "--backup-dir",
        str(args.backup_dir),
    ]


def parse_finish_update_argv(argv: Sequence[str]) -> FinishUpdateArgs | None:
    """Parse finish-update args out of ``argv``, or ``None`` if this isn't finish mode.

    :mod:`collapsarr.__main__` calls this on ``sys.argv[1:]`` at startup: a
    ``None`` return means "ordinary server launch, carry on"; a
    :class:`FinishUpdateArgs` means "this is the handoff process, run
    :func:`finish_native_update` instead of starting the server". Uses
    :func:`argparse.ArgumentParser.parse_known_args` so unrelated arguments are
    ignored rather than rejected, and ``add_help=False`` so it never hijacks
    ``-h``/``--help`` from the normal CLI.
    """
    if FINISH_UPDATE_FLAG not in argv:
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(FINISH_UPDATE_FLAG, action="store_true", dest="finish")
    parser.add_argument("--old-pid", type=int, required=True)
    parser.add_argument("--live-dir", required=True)
    parser.add_argument("--staged-dir", required=True)
    parser.add_argument("--backup-dir", required=True)
    namespace, _ = parser.parse_known_args(list(argv))
    return FinishUpdateArgs(
        old_pid=namespace.old_pid,
        live_dir=Path(namespace.live_dir),
        staged_dir=Path(namespace.staged_dir),
        backup_dir=Path(namespace.backup_dir),
    )


# --- production seams: spawn / exit / re-exec --------------------------------


def _spawn_handoff(args: FinishUpdateArgs, staged_dir: Path) -> None:
    """The real, production :data:`HandoffSpawner`: launch the detached handoff process.

    Runs the *staged* executable (``staged_dir/<exe name>``, the just-verified
    new binary) in finish-update mode, **detached** so it outlives the live
    process about to exit: ``start_new_session=True`` on POSIX gives it its own
    session/process group (the same isolation
    :func:`collapsarr.downmix.cancellation.make_cancellable_runner` uses), and
    ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` does the equivalent on
    Windows. The child is intentionally *not* waited on -- the whole point is
    that it survives us. The executable basename is taken from the running
    process (:data:`sys.executable`), which matches the staged build's binary
    name since both are the same PyInstaller ``COLLECT`` output.
    """
    staged_exe = staged_dir / Path(sys.executable).name
    argv = build_finish_update_argv(str(staged_exe), args)
    if sys.platform == "win32":  # pragma: no cover - Windows-only
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(  # noqa: S603 - argv is our own fixed contract, not shell text
            argv, close_fds=True, creationflags=creationflags
        )
    else:
        subprocess.Popen(  # noqa: S603 - argv is our own fixed contract, not shell text
            argv, close_fds=True, start_new_session=True
        )


def _exit_process() -> None:  # pragma: no cover - terminates the process
    """The real, production :data:`ExitFn`: terminate the live process now.

    Called *only* after :func:`_spawn_handoff` has successfully launched the
    handoff (the AC's "old process exits only after launching the handoff"
    ordering). Uses ``os._exit`` rather than ``sys.exit`` so the exit is
    immediate and unconditional -- there is no cleanup that must run (the DB
    lives in the untouched user-data dir, and the handoff owns everything from
    here), and on Windows a prompt exit is what releases this executable's
    image lock so the handoff's rename can succeed. Never returns; a test's
    :data:`ExitFn` spy returns instead so the flow can be asserted end to end.
    """
    logging.shutdown()
    os._exit(0)


def _reexec_into(live_dir: Path) -> None:  # pragma: no cover - replaces the process image
    """The real, production :data:`ReexecFn`: re-exec from the swapped-in install dir.

    After :func:`swap_install_dir`, ``live_dir`` holds the new install, so the
    handoff process re-execs the *live* executable (``live_dir/<exe name>``) --
    not its own staged path, which has been renamed to ``backup_dir`` -- with a
    clean argv (no finish-update flags), bringing the new code up as an
    ordinary server. ``os.execv`` replaces this process image outright, so it
    never returns on success; a test's :data:`ReexecFn` spy returns instead.
    """
    live_exe = live_dir / Path(sys.executable).name
    os.execv(str(live_exe), [str(live_exe)])  # noqa: S606 - re-launching our own binary


# --- the two flow entry points ----------------------------------------------


def apply_native_update(
    session: Session,
    *,
    target_tag: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
    install_dir: Path | None = None,
    spawn_handoff: HandoffSpawner | None = None,
    exit_fn: ExitFn | None = None,
) -> SelfUpdateApplyOutcome:
    """Download, verify, stage, spawn the handoff, and exit (COL-235, live-process side).

    The native counterpart of
    :func:`collapsarr.self_update.apply.apply_pipx_update`, sharing its guard
    discipline exactly (see both module docstrings). Steps, in order:

    1. Resolve ``(platform, arch)`` -- ``ok=False`` before the guard is taken
       if this platform has no release archive.
    2. Reserve the single-flight guard
       (:func:`~collapsarr.self_update.service.begin_self_update`), stamping the
       running version as the rollback target. Propagates
       :class:`~collapsarr.self_update.service.SelfUpdateAlreadyInProgressError`
       to the caller (mapped to ``409``), never caught here.
    3. Fetch + resolve the ``SHA256SUMS`` entry, then download and
       SHA-256-verify the release archive (bounded by
       :data:`~collapsarr.self_update.apply.MAX_RELEASE_ARCHIVE_BYTES`) --
       ``ok=False`` and the guard cleared on any failure, before anything on
       disk is touched.
    4. Extract the verified archive into a fresh **staging** directory beside
       the install dir (:data:`NATIVE_STAGING_DIRNAME`) -- never into the live
       install -- and resolve the staged install root.
    5. Spawn the detached **handoff** process from the staged tree
       (``spawn_handoff``), passing it the old PID, live/staged/backup dirs.
    6. Advance ``phase`` to
       :data:`~collapsarr.self_update.models.PHASE_AWAITING_HEALTH` **without**
       clearing the guard (handed off across the process boundary), then call
       ``exit_fn`` -- the live process exits, but *only now*, after the handoff
       is safely launched.

    Once the guard is reserved (step 2), every failure funnels through one
    broad ``except Exception`` that clears it back to
    :data:`~collapsarr.self_update.models.PHASE_IDLE`, so no failure can leave
    it stuck. ``install_dir``/``spawn_handoff``/``exit_fn`` default to the real
    :func:`resolve_install_dir`/:func:`_spawn_handoff`/:func:`_exit_process`;
    tests inject a ``tmp_path`` install dir and spies so no real process is
    spawned and this process never actually exits.
    """
    import shutil

    resolved_dir = install_dir if install_dir is not None else resolve_install_dir()
    spawn = spawn_handoff or _spawn_handoff
    do_exit = exit_fn or _exit_process

    resolved = resolve_platform_arch()
    if resolved is None:
        return SelfUpdateApplyOutcome(
            ok=False,
            error="Self-update is not available for this platform/architecture.",
        )
    platform_name, arch = resolved
    version = target_tag[1:] if target_tag.startswith("v") else target_tag

    # Reserve the single-flight guard before any I/O -- a second concurrent
    # trigger is rejected here, never partway through a download (identical
    # reasoning to apply_pipx_update; deliberately outside the try/except so a
    # failure to even take the guard never tries to clear it).
    begin_self_update(session, previous_version=__version__, phase=PHASE_DOWNLOADING)

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
            raise _NativeApplyFailure(
                checksum_result.error or "Failed to resolve the release checksum."
            )

        archive_url = _release_asset_url(target_tag, checksum_result.filename)
        download_result = download_and_verify(
            archive_url,
            checksum_result.sha256,
            timeout=timeout,
            transport=transport,
            max_bytes=MAX_RELEASE_ARCHIVE_BYTES,
        )
        if not download_result.ok or download_result.content is None:
            # Network failure or SHA-256 mismatch -- abort before anything on
            # disk is touched (nothing has been staged yet).
            raise _NativeApplyFailure(
                download_result.error or "Failed to download the release archive."
            )

        set_self_update_phase(session, PHASE_VERIFYING)

        # Stage into a *fresh* directory beside the install dir (same
        # filesystem -> the later rename is atomic). Clear any stale staging/
        # backup dirs left by a previously-interrupted attempt first, so their
        # preconditions (staging fresh, backup absent) hold.
        staging_dir = resolved_dir.parent / NATIVE_STAGING_DIRNAME
        backup_dir = resolved_dir.parent / NATIVE_BACKUP_DIRNAME
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
        try:
            _extract_archive(download_result.content, archive_url, staging_dir)
        except NativeExtractionError as exc:
            raise _NativeApplyFailure(str(exc)) from exc
        staged_root = _resolve_staged_root(staging_dir)

        set_self_update_phase(session, PHASE_APPLYING)

        # Launch the handoff process *before* exiting -- the AC's ordering:
        # "the old process exits only after successfully launching the handoff
        # process from staging."
        handoff_args = FinishUpdateArgs(
            old_pid=os.getpid(),
            live_dir=resolved_dir,
            staged_dir=staged_root,
            backup_dir=backup_dir,
        )
        spawn(handoff_args, staged_root)

        # Hand the guard off across the process boundary (in_progress stays
        # True, phase -> awaiting_health) -- the re-exec'd install continues the
        # state machine from this persisted row; a later ticket clears it.
        set_self_update_phase(session, PHASE_AWAITING_HEALTH)
        logger.info(
            "Native self-update to %s staged and handoff launched; exiting.", target_tag
        )
        do_exit()

        # Reached only when ``exit_fn`` is a test spy that returns instead of
        # terminating the process -- a real _exit_process() never returns.
        return SelfUpdateApplyOutcome(ok=True)
    except Exception as exc:
        # The single place every guarded-region failure funnels through -- an
        # expected _NativeApplyFailure or any genuinely unexpected exception --
        # clearing the guard back to idle so it can never be left stuck. See
        # the module docstring and apply_pipx_update's "No failure can leave
        # the guard stuck" section.
        clear_self_update(session, phase=PHASE_IDLE)
        if not isinstance(exc, _NativeApplyFailure):
            logger.exception(
                "Native self-update apply to %s failed unexpectedly", target_tag
            )
        return SelfUpdateApplyOutcome(ok=False, error=str(exc) or repr(exc))


def finish_native_update(
    args: FinishUpdateArgs,
    *,
    wait_timeout: float = PID_WAIT_TIMEOUT,
    poll_interval: float = PID_POLL_INTERVAL,
    pid_probe: PidProbe = _pid_is_running,
    sleep: Sleep = time.sleep,
    monotonic: Monotonic = time.monotonic,
    swap_fn: SwapFn = swap_install_dir,
    reexec_fn: ReexecFn = _reexec_into,
) -> NativeFinishOutcome:
    """Wait for the old PID, atomically swap, and re-exec (COL-235, handoff side).

    Runs in the detached process :func:`_spawn_handoff` launched from the
    staged tree. In strict order:

    1. **Wait for the old PID to fully exit** (:func:`wait_for_pid_exit`). This
       is the load-bearing gate: if the old process has *not* exited within
       ``wait_timeout``, return ``ok=False`` and **do not swap** -- the install
       directory is left completely untouched (see the module docstring on why
       swapping under a live process is unsafe).
    2. **Atomic swap** (``swap_fn`` -> :func:`swap_install_dir`): old install
       renamed aside to ``backup_dir``, staged tree renamed into ``live_dir``.
       An :class:`OSError` here has already been rolled back inline by
       :func:`swap_install_dir`; it is reported as ``ok=False``.
    3. **Re-exec** from the swapped-in ``live_dir`` (``reexec_fn`` ->
       :func:`_reexec_into`) so the new code comes up serving normally. Never
       returns in production.

    Deliberately touches no database: the persisted guard/phase row lives in
    the (untouched) user-data dir, and the re-exec'd server reads it to
    continue the state machine -- a later health-check/rollback ticket clears
    it. All of wait/swap/re-exec are injectable so a test drives the full flow
    with fakes, no real process wait or ``os.execv``.
    """
    exited = wait_for_pid_exit(
        args.old_pid,
        timeout=wait_timeout,
        poll_interval=poll_interval,
        is_running=pid_probe,
        sleep=sleep,
        monotonic=monotonic,
    )
    if not exited:
        # Old process still alive -- never swap under it. Leave the install dir
        # exactly as it is; the update simply did not complete this time.
        return NativeFinishOutcome(
            ok=False,
            error=(
                f"The previous process (pid {args.old_pid}) did not exit within "
                f"{wait_timeout}s; the install directory was left untouched."
            ),
        )

    try:
        swap_fn(args.live_dir, args.staged_dir, args.backup_dir)
    except OSError as exc:
        # swap_install_dir already rolled the first rename back inline, so the
        # live install is restored -- report the failure without re-execing.
        return NativeFinishOutcome(
            ok=False, error=f"Atomic install-directory swap failed: {exc}"
        )

    logger.info("Native self-update swap complete; re-executing from %s.", args.live_dir)
    reexec_fn(args.live_dir)

    # Reached only when ``reexec_fn`` is a test spy that returns -- a real
    # os.execv() never returns on success.
    return NativeFinishOutcome(ok=True)


def run_finish_update(args: FinishUpdateArgs) -> None:  # pragma: no cover - process-replacing glue
    """Drive :func:`finish_native_update` with production seams for :mod:`collapsarr.__main__`.

    The thin glue :func:`collapsarr.__main__.main` calls when
    :func:`parse_finish_update_argv` recognises finish-update mode. Runs the
    real wait/swap/re-exec; on success the re-exec replaces this process and
    never returns. On failure it logs the reason and exits non-zero -- the old
    install is intact (the swap either never ran or was rolled back), so the
    operator's previous version keeps working.
    """
    outcome = finish_native_update(args)
    if not outcome.ok:
        logger.error("Native self-update finish step failed: %s", outcome.error)
        raise SystemExit(1)


__all__ = [
    "FINISH_UPDATE_FLAG",
    "NATIVE_BACKUP_DIRNAME",
    "NATIVE_QUARANTINE_DIRNAME",
    "NATIVE_STAGING_DIRNAME",
    "PID_POLL_INTERVAL",
    "PID_WAIT_TIMEOUT",
    "ExitFn",
    "FinishUpdateArgs",
    "HandoffSpawner",
    "Monotonic",
    "NativeExtractionError",
    "NativeFinishOutcome",
    "PidProbe",
    "ReexecFn",
    "Sleep",
    "SwapFn",
    "apply_native_update",
    "build_finish_update_argv",
    "finish_native_update",
    "parse_finish_update_argv",
    "resolve_install_dir",
    "restore_from_backup",
    "run_finish_update",
    "swap_install_dir",
    "wait_for_pid_exit",
]
