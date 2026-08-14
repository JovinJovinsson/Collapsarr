"""Tests for the pipx self-update apply flow (COL-232).

Exercised through the bare service-layer ``session`` fixture (no HTTP app),
mirroring ``tests/test_self_update_service.py``'s convention -- this module
has no FastAPI dependency of its own. ``resolve_platform_arch`` is
monkeypatched to a fixed platform/arch so the fixture data (SHA256SUMS
content, expected filename) stays deterministic across the CI matrix's
actual host platform. Both OS-touching seams (``subprocess_runner``,
``reexec_fn``) are fakes/spies throughout -- per the AC, no real subprocess
spawn or re-exec ever happens in this file.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence

import httpx
import pytest
from sqlalchemy.orm import Session

from collapsarr import __version__
from collapsarr.self_update.apply import (
    PIPX_UPGRADE_COMMAND,
    SelfUpdateApplyOutcome,
    apply_pipx_update,
    stable_update_target,
)
from collapsarr.self_update.models import PHASE_AWAITING_HEALTH, PHASE_IDLE, SelfUpdateState
from collapsarr.self_update.service import (
    SelfUpdateAlreadyInProgressError,
    begin_self_update,
    get_self_update_state,
)
from collapsarr.settings.models import UPDATE_CHANNEL_BETA, UPDATE_CHANNEL_STABLE
from collapsarr.update_check.client import GITHUB_REPO
from collapsarr.update_check.models import UpdateCheckState

_TAG = "v1.2.3"
_VERSION = "1.2.3"
_FILENAME = f"collapsarr-{_VERSION}-linux-amd64.tar.gz"
_ARCHIVE_BYTES = b"pretend this is a real release archive's bytes"
_ARCHIVE_SHA256 = hashlib.sha256(_ARCHIVE_BYTES).hexdigest()
_SHA256SUMS_CONTENT = f"{_ARCHIVE_SHA256}  {_FILENAME}\n"


class _RunnerSpy:
    """A fake :data:`~collapsarr.self_update.apply.SubprocessRunner`.

    Records every call and returns a fixed :class:`subprocess.CompletedProcess`
    -- never spawns a real ``pipx`` process.
    """

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[Sequence[str], float]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, timeout))
        return subprocess.CompletedProcess(
            list(command), self.returncode, stdout="", stderr=self.stderr
        )


class _ReexecSpy:
    """A fake :data:`~collapsarr.self_update.apply.ReexecFn`.

    Records that it was called and returns normally -- a real ``os.execv``
    never returns.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def _transport(
    *, sha256sums: str = _SHA256SUMS_CONTENT, archive: bytes = _ARCHIVE_BYTES
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/SHA256SUMS"):
            return httpx.Response(200, text=sha256sums)
        if path.endswith(f"/{_FILENAME}"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def _patch_platform(monkeypatch: pytest.MonkeyPatch, resolved: tuple[str, str] | None) -> None:
    monkeypatch.setattr(
        "collapsarr.self_update.apply.resolve_platform_arch", lambda: resolved
    )


# --------------------------------------------------------------------------- #
# stable_update_target
# --------------------------------------------------------------------------- #


def test_stable_update_target_is_none_before_any_tick_has_run() -> None:
    assert stable_update_target(None, "1.0.0") is None


def test_stable_update_target_is_none_when_no_tag_resolved_yet() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_STABLE, latest_tag=None)
    assert stable_update_target(state, "1.0.0") is None


def test_stable_update_target_is_none_for_a_beta_channel_row() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_BETA, latest_tag="beta-v1.2.3.0007")
    assert stable_update_target(state, "1.0.0") is None


def test_stable_update_target_is_none_when_already_up_to_date() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_STABLE, latest_tag="v1.2.3")
    assert stable_update_target(state, "1.2.3") is None


def test_stable_update_target_returns_the_latest_tag_when_newer() -> None:
    state = UpdateCheckState(channel=UPDATE_CHANNEL_STABLE, latest_tag="v1.2.3")
    assert stable_update_target(state, "1.0.0") == "v1.2.3"


# --------------------------------------------------------------------------- #
# AC: happy path -- download, verify, pipx upgrade, re-exec
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_happy_path(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy(returncode=0)
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert isinstance(outcome, SelfUpdateApplyOutcome)
    assert outcome.ok is True
    assert outcome.error is None

    assert len(runner.calls) == 1
    assert runner.calls[0][0] == PIPX_UPGRADE_COMMAND
    assert reexec.calls == 1

    state = get_self_update_state(session)
    assert state.in_progress is True  # guard handed off across the re-exec, not cleared
    assert state.phase == PHASE_AWAITING_HEALTH
    assert state.previous_version == __version__


def test_apply_pipx_update_requests_the_expected_release_asset_urls(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/SHA256SUMS"):
            return httpx.Response(200, text=_SHA256SUMS_CONTENT)
        return httpx.Response(200, content=_ARCHIVE_BYTES)

    apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=httpx.MockTransport(handler),
        subprocess_runner=_RunnerSpy(),
        reexec_fn=_ReexecSpy(),
    )

    assert f"https://github.com/{GITHUB_REPO}/releases/download/{_TAG}/SHA256SUMS" in seen
    assert (
        f"https://github.com/{GITHUB_REPO}/releases/download/{_TAG}/{_FILENAME}" in seen
    )


# --------------------------------------------------------------------------- #
# AC: checksum mismatch aborts cleanly, never touching pipx upgrade
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_checksum_mismatch_aborts_without_upgrading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    wrong_sha256sums = f"{'0' * 64}  {_FILENAME}\n"
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(sha256sums=wrong_sha256sums),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "sha-256" in outcome.error.lower() or "mismatch" in outcome.error.lower()

    # pipx upgrade must never have been reached, and no re-exec attempted.
    assert runner.calls == []
    assert reexec.calls == 0

    # The running install is untouched -- the guard is cleared back to idle.
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


def test_apply_pipx_update_missing_checksum_entry_aborts_without_upgrading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        # No SHA256SUMS entry for this release at all.
        transport=_transport(sha256sums=""),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# AC: a second, concurrent trigger is rejected
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_rejects_a_concurrent_trigger(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    begin_self_update(session, previous_version="0.9.0")
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    with pytest.raises(SelfUpdateAlreadyInProgressError):
        apply_pipx_update(
            session,
            target_tag=_TAG,
            transport=_transport(),
            subprocess_runner=runner,
            reexec_fn=reexec,
        )

    # No network/subprocess/re-exec activity from the rejected attempt.
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.previous_version == "0.9.0"  # the first attempt's, untouched


# --------------------------------------------------------------------------- #
# pipx upgrade itself fails (non-zero exit)
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_reports_a_failed_pipx_upgrade_without_reexecing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy(returncode=1, stderr="pipx: package not found")
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "exit 1" in outcome.error
    assert "pipx: package not found" in outcome.error
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# Code-review must-fix: any unexpected exception inside the guarded region
# clears the guard too, not just the specific failure modes handled above --
# a poisoned in_progress=True row would otherwise block every future
# self-update attempt (begin_self_update refuses whenever it's already set),
# and it's a persisted row that would survive a process restart.
# --------------------------------------------------------------------------- #


class _RaisingRunner:
    """A fake :data:`~collapsarr.self_update.apply.SubprocessRunner` that raises.

    ``PermissionError`` (a real-world case: a ``pipx`` on ``PATH`` that
    exists but isn't executable) is deliberately *not* one of the two
    exception types :func:`~collapsarr.self_update.apply.apply_pipx_update`
    special-cases (``FileNotFoundError``/``subprocess.TimeoutExpired``), so
    this exercises the broad catch-all rather than either specific branch.
    """

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(
        self, command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        raise self.exc


def test_apply_pipx_update_clears_the_guard_on_an_unexpected_subprocess_exception(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RaisingRunner(PermissionError("[Errno 13] Permission denied: 'pipx'"))
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert "permission" in outcome.error.lower()
    assert runner.calls == 1
    assert reexec.calls == 0

    # The must-fix: the guard is cleared, not left poisoned -- a subsequent
    # attempt must be able to start.
    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE

    # Proof the guard genuinely recovered: a fresh attempt can begin.
    begin_self_update(session, previous_version="1.0.0")


def test_apply_pipx_update_clears_the_guard_on_an_unexpected_reexec_exception(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, ("linux", "amd64"))
    runner = _RunnerSpy(returncode=0)

    def _exploding_reexec() -> None:
        raise OSError("exec format error")

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=_exploding_reexec,
    )

    assert outcome.ok is False
    assert outcome.error is not None
    assert len(runner.calls) == 1  # pipx upgrade did run before the re-exec blew up

    state = get_self_update_state(session)
    assert state.in_progress is False
    assert state.phase == PHASE_IDLE


# --------------------------------------------------------------------------- #
# Unsupported platform
# --------------------------------------------------------------------------- #


def test_apply_pipx_update_unsupported_platform_fails_before_taking_the_guard(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_platform(monkeypatch, None)
    runner = _RunnerSpy()
    reexec = _ReexecSpy()

    outcome = apply_pipx_update(
        session,
        target_tag=_TAG,
        transport=_transport(),
        subprocess_runner=runner,
        reexec_fn=reexec,
    )

    assert outcome.ok is False
    assert runner.calls == []
    assert reexec.calls == 0

    state = get_self_update_state(session)
    assert isinstance(state, SelfUpdateState)
    assert state.in_progress is False  # the guard was never even reserved
