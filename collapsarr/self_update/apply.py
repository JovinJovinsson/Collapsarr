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

**Scope.** Deliberately covers only the "no Jobs running" case (COL-232's
own ticket title): neither **Wait & Restart**'s job-drain nor **Cancel &
Restart Now**'s hard-cancel (``CONTEXT.md``'s **Self-Update** entry) is
implemented here -- a later ticket in this epic adds the Jobs-in-flight
branches. This module's caller (:mod:`collapsarr.self_update.routes`) does
not check the Job Queue at all.

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
from ..settings.models import UPDATE_CHANNEL_STABLE
from ..update_check.client import GITHUB_REPO
from ..update_check.comparison import is_up_to_date
from ..update_check.models import UpdateCheckState
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
    3. Fetch + resolve the ``SHA256SUMS`` entry for ``target_tag``
       (:func:`~collapsarr.self_update.client.fetch_checksum_entry`).
       ``ok=False`` and the guard is cleared back to
       :data:`~collapsarr.self_update.models.PHASE_IDLE` if no entry exists.
    4. Download and SHA-256-verify the resolved archive
       (:func:`~collapsarr.ffmpeg_download.client.download_and_verify`,
       bounded by :data:`MAX_RELEASE_ARCHIVE_BYTES`). ``ok=False`` and the
       guard is cleared on any network failure *or* a checksum mismatch --
       ``pipx upgrade`` is never reached on this path (the AC's "verify
       before upgrade, strictly sequential" requirement).
    5. Run :data:`PIPX_UPGRADE_COMMAND` via ``subprocess_runner`` (defaults
       to :func:`_run_pipx_upgrade`). ``ok=False`` and the guard is cleared
       on a missing ``pipx`` executable, a timeout, or a non-zero exit --
       the running install is provably untouched by every earlier abort
       path, and here the failure is ``pipx``'s own to report.
    6. On a successful upgrade, advance ``phase`` to
       :data:`~collapsarr.self_update.models.PHASE_AWAITING_HEALTH` --
       **without** clearing ``in_progress`` (see the module docstring's
       "Guard handoff" section) -- and call ``reexec_fn`` (defaults to
       :func:`_reexec_process`).

    Once the guard is reserved (step 2), every failure -- expected or not --
    is funneled through one ``except Exception`` clause that clears it back
    to idle; see the module docstring's "No failure can leave the guard
    stuck" section. ``transport`` is forwarded to every network call this
    makes (tests inject an ``httpx.MockTransport``; production leaves it
    ``None`` for a real network call), matching every other
    transport-injectable seam in this codebase.
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


__all__ = [
    "DOWNLOAD_TIMEOUT",
    "MAX_RELEASE_ARCHIVE_BYTES",
    "PIPX_UPGRADE_COMMAND",
    "PIPX_UPGRADE_TIMEOUT",
    "ReexecFn",
    "SelfUpdateApplyOutcome",
    "SubprocessRunner",
    "apply_pipx_update",
    "stable_update_target",
]
