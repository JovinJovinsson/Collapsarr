"""Post-re-exec health-check gate + pinned-reinstall rollback (COL-234).

COL-232's ``apply_pipx_update`` hands off across its ``os.execv`` re-exec by
advancing ``phase`` to :data:`~collapsarr.self_update.models.
PHASE_AWAITING_HEALTH` and deliberately leaving the in-progress guard held
(see :mod:`collapsarr.self_update.apply`'s module docstring) -- the freshly
re-exec'd process is left to pick that state back up and either confirm the
new build is healthy or roll back. :func:`resolve_awaiting_health` is that
pickup: a no-op on every ordinary boot (``phase`` is anything other than
``awaiting_health``), and on the one boot that follows a re-exec, it either
clears the guard back to :data:`~collapsarr.self_update.models.PHASE_IDLE`
(healthy -- the update stands) or reinstalls the exact previous version by
pin (``pip install collapsarr==<previous_version>``) and re-execs into it,
leaving :data:`~collapsarr.self_update.models.PHASE_ROLLED_BACK` for the
status endpoint (:mod:`collapsarr.self_update.routes`) to report -- its
existing ``phase``/``previous_version`` fields already say everything a
"update failed, rolled back to vX.Y.Z" banner needs; no response-shape change
required.

**Design decision: why this runs from process startup, not a supervisor
watching the re-exec.** COL-232's apply flow does an in-process ``os.execv``
re-exec, not a fork+exec with a separate supervisor process -- there is
nothing left running on the other side of the re-exec to watch it. The only
process that can ever observe "did the re-exec'd build come up healthy" is
therefore the re-exec'd process itself, checking on its own startup. This
module is wired into :func:`collapsarr.main.create_app`'s lifespan, right
after its synchronous first Health Check Framework tick
(``health_scheduler.run_once()``) -- the same one that populates the state
``GET /health`` reads -- so the very first read this function makes is
already fresh.

**Design decision: an in-process check, not a real HTTP call to ``/health``.**
A tempting alternative is to literally poll ``GET /health`` over a loopback
socket. That deadlocks here: Uvicorn binds the listening socket *before*
running ASGI lifespan startup, but does not start accepting/serving
connections on it until lifespan startup returns -- a blocking (or even an
``await``ed async) HTTP call made *from inside* lifespan startup would sit in
the accept queue forever, since the only event loop that could ever service
it is busy running this exact function. Sidestepped entirely by checking
in-process: :data:`HealthCheckFn` is a plain ``() -> bool`` seam, and this
module's caller (:mod:`collapsarr.main`) wires the production one to rerun
the Health Check Framework's own checks and read back
:func:`collapsarr.health.service.list_failing_checks` -- the exact same
signal ``GET /health``'s ``status`` field is built from -- without ever
opening a socket to itself.

**Retries, not one shot.** A check immediately after re-exec can catch a
transient condition (e.g. an Arr instance that takes a moment to answer)
that would resolve on its own -- :func:`resolve_awaiting_health` calls
``health_check_fn`` up to ``max_attempts`` times, sleeping ``poll_interval``
between attempts (via the injectable ``sleep_fn``, so tests never really
sleep), before concluding the new build is unhealthy and rolling back.

**No failure can leave the guard stuck**, mirroring
:mod:`collapsarr.self_update.apply`'s own "no failure can leave the guard
stuck" section exactly: once ``phase`` is confirmed to be
``awaiting_health``, the whole check/rollback/relaunch sequence runs inside
one ``try``/``except Exception`` block. Every *expected* failure (no
``previous_version`` recorded, a missing ``pip`` executable, a reinstall
timeout, or a non-zero ``pip install`` exit) raises the internal
:class:`_HealthGateFailure`; any other, genuinely unexpected exception is
caught too. Both funnel through the same ``except`` clause, which clears the
guard back to :data:`~collapsarr.self_update.models.PHASE_IDLE` -- a failed
rollback attempt must never leave ``in_progress`` permanently held, or every
future self-update attempt would be refused
(:class:`~collapsarr.self_update.service.SelfUpdateAlreadyInProgressError`)
with no recovery short of a manual DB edit. ``PHASE_ROLLED_BACK`` is only
ever the *outcome* of a rollback whose pinned reinstall genuinely succeeded;
a reinstall that itself fails is a distinct, rarer failure mode this ticket
does not give its own phase -- it settles back to ``idle`` like every other
unexpected failure, logged loudly (``logger.exception``) so it's visible in
the log tail (``GET /api/system/logs``, COL-131) even though the status
endpoint can't distinguish it from "never attempted."
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .apply import ReexecFn, SubprocessRunner
from .models import PHASE_AWAITING_HEALTH, PHASE_IDLE, PHASE_ROLLED_BACK
from .service import clear_self_update, get_self_update_state

logger = logging.getLogger(__name__)

#: Injectable "is the freshly re-exec'd build healthy" seam -- see the module
#: docstring's "an in-process check, not a real HTTP call" section for why
#: this is a plain zero-argument predicate rather than an HTTP client call.
#: Tests inject a fake/spy; :mod:`collapsarr.main` wires the real one.
HealthCheckFn = Callable[[], bool]

#: Number of times :func:`resolve_awaiting_health` calls ``health_check_fn``
#: before concluding the new build is unhealthy -- together with
#: :data:`DEFAULT_HEALTH_CHECK_POLL_INTERVAL` this is the "timeout" budget
#: the AC describes (roughly 30s at the defaults), spent giving a transient
#: just-booted condition a chance to resolve on its own before rolling back.
DEFAULT_HEALTH_CHECK_MAX_ATTEMPTS = 6

#: Seconds slept (via the injectable ``sleep_fn``) between failed attempts.
DEFAULT_HEALTH_CHECK_POLL_INTERVAL = 5.0

#: ``pip install collapsarr==<previous_version>`` is a local pip-resolver
#: operation, not a large download -- matches
#: :data:`collapsarr.self_update.apply.PIPX_UPGRADE_TIMEOUT`'s budget for the
#: forward-upgrade equivalent.
PIP_ROLLBACK_TIMEOUT = 120.0


class _HealthGateFailure(RuntimeError):
    """Internal control-flow signal: an expected failure inside the rollback.

    Raised for every one of :func:`resolve_awaiting_health`'s *expected*
    rollback failure modes (no ``previous_version`` recorded, a missing
    ``pip`` executable, a reinstall timeout, a non-zero ``pip install``
    exit) so they funnel through the exact same ``except`` clause as any
    genuinely unexpected exception -- see the module docstring's "No failure
    can leave the guard stuck" section. Never escapes
    :func:`resolve_awaiting_health` itself.
    """


@dataclass(frozen=True, slots=True)
class SelfUpdateHealthGateOutcome:
    """Outcome of one :func:`resolve_awaiting_health` call.

    ``action`` is one of:

    * ``"noop"`` -- ``phase`` wasn't :data:`~collapsarr.self_update.models.
      PHASE_AWAITING_HEALTH`; the ordinary case on every boot that doesn't
      follow a self-update re-exec.
    * ``"healthy"`` -- the health check passed within the attempt budget;
      the guard is cleared back to
      :data:`~collapsarr.self_update.models.PHASE_IDLE`.
    * ``"rolled_back"`` -- the health check never passed; the pinned
      reinstall succeeded and ``reexec_fn`` was called (only actually
      observed by a caller when ``reexec_fn`` is a test fake that returns
      instead of replacing the process).
    * ``"rollback_failed"`` -- the health check never passed *and* the
      rollback itself failed (or something else raised unexpectedly); the
      guard is still cleared back to ``idle`` (never left stuck), and
      ``error`` carries a short human-readable reason.
    """

    action: str
    error: str | None = None


def _pip_install_pinned_command(version: str) -> tuple[str, ...]:
    """Build the pinned-reinstall command the AC names verbatim.

    Deliberately plain ``pip install`` -- not ``pipx install`` -- per this
    ticket's own instructions: a pipx-managed venv already has a ``pip``
    available inside it, and re-pointing the *existing* venv at the pinned
    version in place is exactly what a rollback wants (mirrors what ``pipx
    upgrade`` itself does under the hood for the forward direction).
    """
    return ("pip", "install", f"collapsarr=={version}")


def _run_pip_install(command: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    """The real, production :data:`~collapsarr.self_update.apply.SubprocessRunner`
    for the pinned reinstall -- a thin wrapper around :func:`subprocess.run`,
    matching :func:`collapsarr.self_update.apply._run_pipx_upgrade`'s shape
    exactly (see that function's docstring)."""
    return subprocess.run(  # noqa: S603 - command is built from a fixed literal + version, not shell text
        list(command), capture_output=True, text=True, timeout=timeout, check=False
    )


def _relaunch_process() -> None:
    """The real, production :data:`~collapsarr.self_update.apply.ReexecFn` for
    relaunching into the rolled-back version -- identical in shape and intent
    to :func:`collapsarr.self_update.apply._reexec_process` (duplicated
    rather than imported across module-privacy boundaries; see that
    function's docstring for the full rationale, which applies unchanged
    here)."""
    os.execv(sys.argv[0], sys.argv)  # noqa: S606 - restarting this exact process, not an arbitrary command


def resolve_awaiting_health(
    session: Session,
    *,
    health_check_fn: HealthCheckFn,
    subprocess_runner: SubprocessRunner | None = None,
    reexec_fn: ReexecFn | None = None,
    max_attempts: int = DEFAULT_HEALTH_CHECK_MAX_ATTEMPTS,
    poll_interval: float = DEFAULT_HEALTH_CHECK_POLL_INTERVAL,
    sleep_fn: Callable[[float], None] = time.sleep,
    subprocess_timeout: float = PIP_ROLLBACK_TIMEOUT,
) -> SelfUpdateHealthGateOutcome:
    """Confirm health after a self-update re-exec, or roll back (COL-234).

    Called once, early in every process boot (see
    :func:`collapsarr.main.create_app`'s lifespan) -- a no-op
    (``action="noop"``) unless the persisted phase is exactly
    :data:`~collapsarr.self_update.models.PHASE_AWAITING_HEALTH`, i.e. this
    boot is the one immediately following COL-232's apply-flow re-exec.

    Calls ``health_check_fn`` up to ``max_attempts`` times, sleeping
    ``poll_interval`` seconds (via ``sleep_fn``) between failed attempts. On
    success, clears the guard to
    :data:`~collapsarr.self_update.models.PHASE_IDLE` -- the update stands,
    exactly as if it had never needed watching. On a health check that never
    passes within the attempt budget, reinstalls the exact previous version
    by pin via ``subprocess_runner`` (defaults to :func:`_run_pip_install`)
    and calls ``reexec_fn`` (defaults to :func:`_relaunch_process`) to
    relaunch into it, leaving
    :data:`~collapsarr.self_update.models.PHASE_ROLLED_BACK` (guard cleared,
    ``previous_version`` left in place) for the status endpoint to report.
    See the module docstring's "No failure can leave the guard stuck" section
    for how every failure along this path -- including a reinstall that
    itself fails -- is guaranteed not to leave the guard permanently held.
    """
    row = get_self_update_state(session)
    if row.phase != PHASE_AWAITING_HEALTH:
        return SelfUpdateHealthGateOutcome(action="noop")

    run = subprocess_runner or _run_pip_install
    reexec = reexec_fn or _relaunch_process
    previous_version = row.previous_version

    try:
        healthy = False
        for attempt in range(max_attempts):
            if health_check_fn():
                healthy = True
                break
            if attempt < max_attempts - 1:
                sleep_fn(poll_interval)

        if healthy:
            clear_self_update(session, phase=PHASE_IDLE)
            logger.info(
                "Self-update health check passed after re-exec; now running the new version."
            )
            return SelfUpdateHealthGateOutcome(action="healthy")

        if not previous_version:
            raise _HealthGateFailure(
                "Health check failed after re-exec, but no previous_version is recorded "
                "to roll back to."
            )

        logger.warning(
            "Self-update health check failed after re-exec (%d attempt(s)); "
            "rolling back to %s.",
            max_attempts,
            previous_version,
        )
        command = _pip_install_pinned_command(previous_version)
        try:
            result = run(command, subprocess_timeout)
        except FileNotFoundError as exc:
            raise _HealthGateFailure("pip executable not found on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise _HealthGateFailure(
                f"Pinned reinstall of collapsarr=={previous_version} timed out "
                f"after {subprocess_timeout}s."
            ) from exc

        if result.returncode != 0:
            raise _HealthGateFailure(
                f"Pinned reinstall of collapsarr=={previous_version} failed "
                f"(exit {result.returncode}): {result.stderr}"
            )

        # Pinned reinstall succeeded -- the previous version is back on disk.
        # Settle the terminal failure phase and relaunch into it.
        clear_self_update(session, phase=PHASE_ROLLED_BACK)
        logger.info("Rolled back to collapsarr==%s; re-executing.", previous_version)
        reexec()

        # Reached only when `reexec` is a test fake/spy that returns instead
        # of replacing the process -- a real os.execv() never returns on
        # success.
        return SelfUpdateHealthGateOutcome(action="rolled_back")
    except Exception as exc:
        # Everything from here down is the single place every failure along
        # this path -- a deliberate _HealthGateFailure raised above, or any
        # other, genuinely unexpected exception -- funnels through. See the
        # module docstring's "No failure can leave the guard stuck" section.
        if not isinstance(exc, _HealthGateFailure):
            logger.exception(
                "Self-update health gate failed unexpectedly while awaiting_health"
            )
        clear_self_update(session, phase=PHASE_IDLE)
        return SelfUpdateHealthGateOutcome(action="rollback_failed", error=str(exc) or repr(exc))


__all__ = [
    "DEFAULT_HEALTH_CHECK_MAX_ATTEMPTS",
    "DEFAULT_HEALTH_CHECK_POLL_INTERVAL",
    "PIP_ROLLBACK_TIMEOUT",
    "HealthCheckFn",
    "SelfUpdateHealthGateOutcome",
    "resolve_awaiting_health",
]
