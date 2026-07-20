"""Tests for the FFmpeg startup health check and its notifier bridge (COL-38).

``check_ffmpeg`` is a pure ``shutil.which`` presence check (unit tests below
resolve a real, guaranteed-missing binary name rather than mocking
``shutil.which``, to exercise the real lookup). ``notify_ffmpeg_missing``
mirrors ``collapsarr.jobs.failure_notify.notify_job_failure``'s bridge
pattern -- see ``tests/test_jobs_failure_notify.py`` for the sibling suite --
and is driven the same way: an ``httpx.MockTransport`` so no live network call
is made.
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy.orm import Session

from collapsarr.health import FfmpegCheckResult, check_ffmpeg, notify_ffmpeg_missing
from collapsarr.notify.service import update_notifier_config

_MISSING_BINARY = "collapsarr-test-definitely-not-a-real-binary"


def _ok_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler), seen


# ---------------------------------------------------------------------------
# check_ffmpeg: present + missing paths.
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
# notify_ffmpeg_missing: no-op when available, dispatches when missing.
# ---------------------------------------------------------------------------


def test_notify_ffmpeg_missing_is_a_noop_when_ffmpeg_is_available(session: Session) -> None:
    check = FfmpegCheckResult(
        available=True, ffmpeg_path="ffmpeg", detail="FFmpeg found at '/usr/bin/ffmpeg'."
    )
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    notify_ffmpeg_missing(session, check, transport=transport)

    assert seen == []


def test_notify_ffmpeg_missing_dispatches_to_enabled_webhook(session: Session) -> None:
    check = FfmpegCheckResult(
        available=False,
        ffmpeg_path="ffmpeg",
        detail="FFmpeg executable 'ffmpeg' was not found on PATH.",
    )
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)
    transport, seen = _ok_transport()

    notify_ffmpeg_missing(session, check, transport=transport)

    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["event_type"] == "health_check_failed"
    assert payload["details"]["ffmpeg_path"] == "ffmpeg"
    assert payload["details"]["detail"] == check.detail


def test_notify_ffmpeg_missing_dispatches_to_discord_too(session: Session) -> None:
    check = FfmpegCheckResult(available=False, ffmpeg_path="ffmpeg", detail="missing")
    update_notifier_config(
        session,
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        discord_enabled=True,
    )
    transport, seen = _ok_transport()

    notify_ffmpeg_missing(session, check, transport=transport)

    assert len(seen) == 1
    embed = json.loads(seen[0].content)["embeds"][0]
    assert embed["title"] == "FFmpeg not found"


def test_notify_ffmpeg_missing_makes_no_network_call_when_no_notifier_enabled(
    session: Session,
) -> None:
    check = FfmpegCheckResult(available=False, ffmpeg_path="ffmpeg", detail="missing")
    transport, seen = _ok_transport()

    notify_ffmpeg_missing(session, check, transport=transport)  # must not raise

    assert seen == []


def test_notify_ffmpeg_missing_swallows_a_connection_error(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    check = FfmpegCheckResult(available=False, ffmpeg_path="ffmpeg", detail="missing")
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)

    notify_ffmpeg_missing(session, check, transport=httpx.MockTransport(handler))  # must not raise


def test_notify_ffmpeg_missing_swallows_an_http_error_status(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    check = FfmpegCheckResult(available=False, ffmpeg_path="ffmpeg", detail="missing")
    update_notifier_config(session, webhook_url="https://example.com/hook", webhook_enabled=True)

    notify_ffmpeg_missing(session, check, transport=httpx.MockTransport(handler))  # must not raise
