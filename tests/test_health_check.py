"""Tests for the FFmpeg probe, its framework adapter, and state reconciliation (COL-75).

``check_ffmpeg`` is a pure ``shutil.which`` presence check (unit tests below
resolve a real, guaranteed-missing binary name rather than mocking
``shutil.which``, to exercise the real lookup). ``make_ffmpeg_check_run`` adapts
it into the shared :class:`HealthCheckResult` shape. ``reconcile_health_results``
diffs a tick's results against persisted state and fires transition
notifications through the generic notifier fan-out -- driven here with an
``httpx.MockTransport`` so no live network call is made, the same idiom the
retired ``notify_ffmpeg_missing`` suite used.
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.health import (
    CHECK_STATUS_FAILING,
    CHECK_STATUS_PASSING,
    FFMPEG_MISSING_CODE,
    SEVERITY_ERROR,
    FfmpegCheckResult,
    HealthCheckContext,
    HealthCheckResult,
    check_ffmpeg,
    list_failing_checks,
    list_health_check_states,
    make_ffmpeg_check_run,
    reconcile_health_results,
)
from collapsarr.notify.service import update_notifier_config
from collapsarr.settings.service import get_global_settings

_MISSING_BINARY = "collapsarr-test-definitely-not-a-real-binary"


def _ok_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler), seen


def _failing(code: str = "TEST-001", *, instance_id: int | None = None) -> HealthCheckResult:
    return HealthCheckResult.failed(
        code=code, category="test", severity=SEVERITY_ERROR, message="down", instance_id=instance_id
    )


def _passing(code: str = "TEST-001", *, instance_id: int | None = None) -> HealthCheckResult:
    return HealthCheckResult.ok(
        code=code, category="test", severity=SEVERITY_ERROR, message="up", instance_id=instance_id
    )


# ---------------------------------------------------------------------------
# check_ffmpeg: present + missing paths (unchanged signature/return type).
# ---------------------------------------------------------------------------


def test_check_ffmpeg_reports_available_when_found_on_path() -> None:
    """`ffmpeg` is expected to be installed in the dev/CI environment (the
    downmix pipeline's own tests already rely on this)."""
    result = check_ffmpeg()

    assert result.available is True
    assert result.ffmpeg_path == "ffmpeg"
    assert "found" in result.detail.lower()


def test_check_ffmpeg_reports_unavailable_when_not_found_on_path() -> None:
    result = check_ffmpeg(_MISSING_BINARY)

    assert result.available is False
    assert result.ffmpeg_path == _MISSING_BINARY
    assert _MISSING_BINARY in result.detail
    assert "not found" in result.detail.lower()


# ---------------------------------------------------------------------------
# FFmpeg framework adapter: probe -> shared HealthCheckResult shape.
# ---------------------------------------------------------------------------


def _context(session: Session) -> HealthCheckContext:
    return HealthCheckContext(settings=Settings(), session=session)


def test_ffmpeg_check_run_adapts_a_present_binary_to_a_passing_result(session: Session) -> None:
    present = FfmpegCheckResult(
        available=True, ffmpeg_path="ffmpeg", detail="FFmpeg found at '/x'."
    )
    run = make_ffmpeg_check_run(lambda: present)

    results = list(run(_context(session)))

    assert len(results) == 1
    result = results[0]
    assert result.code == FFMPEG_MISSING_CODE
    assert result.severity == SEVERITY_ERROR
    assert result.passing is True
    assert result.instance_id is None
    assert result.message == present.detail


def test_ffmpeg_check_run_adapts_a_missing_binary_to_a_failing_result(session: Session) -> None:
    missing = FfmpegCheckResult(
        available=False, ffmpeg_path="ffmpeg", detail="FFmpeg executable 'ffmpeg' was not found."
    )
    run = make_ffmpeg_check_run(lambda: missing)

    result = list(run(_context(session)))[0]

    assert result.code == FFMPEG_MISSING_CODE
    assert result.passing is False
    assert result.message == missing.detail


# ---------------------------------------------------------------------------
# GlobalSettings.ffmpeg_path wiring (COL-218): the real, un-overridden checker
# reads context.session for a configured override; an injected fake checker
# (as above) keeps ignoring context entirely -- unchanged pre-COL-218 behaviour.
# ---------------------------------------------------------------------------


def test_ffmpeg_check_run_probes_the_configured_path_when_set(session: Session) -> None:
    """With no ``checker`` override, a configured ``GlobalSettings.ffmpeg_path``
    is checked instead of the bare ``"ffmpeg"`` default -- no restart required."""
    row = get_global_settings(session)
    row.ffmpeg_path = _MISSING_BINARY
    session.commit()

    run = make_ffmpeg_check_run()  # real checker, no override

    result = list(run(_context(session)))[0]

    assert result.code == FFMPEG_MISSING_CODE
    assert result.passing is False
    assert _MISSING_BINARY in result.message


def test_ffmpeg_check_run_falls_back_to_bare_default_when_unset(session: Session) -> None:
    """Unset (a fresh install's row, and every existing install's after the
    additive migration): behaviour is byte-for-byte the pre-COL-218 default --
    the bare ``"ffmpeg"`` command resolved off PATH."""
    row = get_global_settings(session)
    assert row.ffmpeg_path is None

    run = make_ffmpeg_check_run()  # real checker, no override

    result = list(run(_context(session)))[0]

    assert result.code == FFMPEG_MISSING_CODE
    assert result.passing is True
    assert result.message == check_ffmpeg().detail


# ---------------------------------------------------------------------------
# reconcile_health_results: persistence + transition detection.
# ---------------------------------------------------------------------------


def test_new_failing_result_creates_a_failing_row_and_notifies(session: Session) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    reconcile_health_results(session, [_failing()], transport=transport)

    rows = list_health_check_states(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == CHECK_STATUS_FAILING
    assert row.first_failed_at is not None
    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["event_type"] == "health_check_failed"
    assert payload["details"]["code"] == "TEST-001"


def test_still_failing_does_not_notify_again_and_keeps_first_failed_at(session: Session) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    reconcile_health_results(session, [_failing()], transport=transport)
    first_failed_at = list_health_check_states(session)[0].first_failed_at
    assert len(seen) == 1

    # A second, still-failing tick: no new notification, streak start preserved.
    reconcile_health_results(session, [_failing()], transport=transport)

    assert len(seen) == 1
    assert list_health_check_states(session)[0].first_failed_at == first_failed_at


def test_fail_to_pass_transition_notifies_recovery_and_clears_first_failed_at(
    session: Session,
) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    reconcile_health_results(session, [_failing()], transport=transport)
    reconcile_health_results(session, [_passing()], transport=transport)

    row = list_health_check_states(session)[0]
    assert row.status == CHECK_STATUS_PASSING
    assert row.first_failed_at is None
    assert len(seen) == 2
    assert json.loads(seen[1].content)["event_type"] == "health_check_recovered"


def test_pass_to_fail_after_passing_notifies_failure(session: Session) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    # First-ever tick passing: a row is created, no notification.
    reconcile_health_results(session, [_passing()], transport=transport)
    assert seen == []
    assert list_health_check_states(session)[0].status == CHECK_STATUS_PASSING

    reconcile_health_results(session, [_failing()], transport=transport)

    assert len(seen) == 1
    assert json.loads(seen[0].content)["event_type"] == "health_check_failed"


def test_passing_never_failed_creates_a_row_without_notifying(session: Session) -> None:
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    reconcile_health_results(session, [_passing()], transport=transport)

    assert seen == []
    assert list_failing_checks(session) == []
    assert len(list_health_check_states(session)) == 1


def test_reconcile_makes_no_network_call_when_no_notifier_enabled(session: Session) -> None:
    transport, seen = _ok_transport()

    reconcile_health_results(session, [_failing()], transport=transport)  # must not raise

    assert seen == []
    assert list_health_check_states(session)[0].status == CHECK_STATUS_FAILING


def test_reconcile_swallows_a_connection_error(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)

    # A notifier problem must never propagate out of reconcile.
    reconcile_health_results(session, [_failing()], transport=httpx.MockTransport(handler))

    assert list_health_check_states(session)[0].status == CHECK_STATUS_FAILING


def test_per_instance_keys_are_tracked_independently(session: Session) -> None:
    """Same code, two instance ids -> two rows, transitioning independently."""
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    reconcile_health_results(
        session,
        [_failing("ERR-CONN-001", instance_id=1), _passing("ERR-CONN-001", instance_id=2)],
        transport=transport,
    )

    rows = {(r.code, r.instance_id): r for r in list_health_check_states(session)}
    assert rows[("ERR-CONN-001", 1)].status == CHECK_STATUS_FAILING
    assert rows[("ERR-CONN-001", 2)].status == CHECK_STATUS_PASSING
    # Only the failing instance notified.
    assert len(seen) == 1
    assert json.loads(seen[0].content)["details"]["instance_id"] == "1"
