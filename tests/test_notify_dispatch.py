"""Tests for the notification dispatch fan-out service (COL-35).

Every case is driven by an ``httpx.MockTransport`` -- no live network call is
made, matching the pattern in ``test_arr_client.py``.
"""

from __future__ import annotations

import httpx
import pytest

from collapsarr.notify.dispatch import (
    DISCORD_NOTIFIER,
    WEBHOOK_NOTIFIER,
    NotificationEvent,
    NotifierDispatchResult,
    dispatch_notification,
)
from collapsarr.notify.models import NotifierConfig


def _config(**overrides: object) -> NotifierConfig:
    defaults: dict[str, object] = {
        "webhook_url": None,
        "webhook_enabled": False,
        "discord_webhook_url": None,
        "discord_enabled": False,
    }
    defaults.update(overrides)
    return NotifierConfig(**defaults)


_EVENT = NotificationEvent(
    event_type="downmix_failure",
    title="Downmix failed",
    message="FFmpeg exited non-zero for The Show S01E01",
    details={"file": "/media/tv/The Show/S01E01.mkv"},
)


def _ok_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    return httpx.MockTransport(handler), seen


# ---------------------------------------------------------------------------
# Fan-out: which notifiers get called.
# ---------------------------------------------------------------------------


def test_dispatch_sends_to_no_notifiers_when_none_enabled() -> None:
    config = _config()

    results = dispatch_notification(config, _EVENT, transport=_ok_transport()[0])

    assert results == []


def test_dispatch_sends_only_to_enabled_webhook() -> None:
    transport, seen = _ok_transport()
    config = _config(webhook_url="https://example.com/hook", webhook_enabled=True)

    results = dispatch_notification(config, _EVENT, transport=transport)

    assert results == [NotifierDispatchResult(notifier=WEBHOOK_NOTIFIER, ok=True)]
    assert len(seen) == 1
    assert str(seen[0].url) == "https://example.com/hook"


def test_dispatch_sends_only_to_enabled_discord() -> None:
    transport, seen = _ok_transport()
    config = _config(
        discord_webhook_url="https://discord.com/api/webhooks/1/abc", discord_enabled=True
    )

    results = dispatch_notification(config, _EVENT, transport=transport)

    assert results == [NotifierDispatchResult(notifier=DISCORD_NOTIFIER, ok=True)]
    assert len(seen) == 1
    assert str(seen[0].url) == "https://discord.com/api/webhooks/1/abc"


def test_dispatch_fans_out_to_both_enabled_notifiers() -> None:
    transport, seen = _ok_transport()
    config = _config(
        webhook_url="https://example.com/hook",
        webhook_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        discord_enabled=True,
    )

    results = dispatch_notification(config, _EVENT, transport=transport)

    assert {r.notifier for r in results} == {WEBHOOK_NOTIFIER, DISCORD_NOTIFIER}
    assert all(r.ok for r in results)
    assert len(seen) == 2


def test_dispatch_does_not_call_a_disabled_notifier_even_with_a_url_configured() -> None:
    transport, seen = _ok_transport()
    config = _config(webhook_url="https://example.com/hook", webhook_enabled=False)

    results = dispatch_notification(config, _EVENT, transport=transport)

    assert results == []
    assert len(seen) == 0


# ---------------------------------------------------------------------------
# Payload shape.
# ---------------------------------------------------------------------------


def test_generic_webhook_payload_carries_event_fields() -> None:
    transport, seen = _ok_transport()
    config = _config(webhook_url="https://example.com/hook", webhook_enabled=True)

    dispatch_notification(config, _EVENT, transport=transport)

    body = seen[0].content
    import json

    payload = json.loads(body)
    assert payload["event_type"] == "downmix_failure"
    assert payload["title"] == "Downmix failed"
    assert payload["message"] == "FFmpeg exited non-zero for The Show S01E01"
    assert payload["details"] == {"file": "/media/tv/The Show/S01E01.mkv"}


def test_discord_webhook_payload_uses_embeds_shape() -> None:
    import json

    transport, seen = _ok_transport()
    config = _config(
        discord_webhook_url="https://discord.com/api/webhooks/1/abc", discord_enabled=True
    )

    dispatch_notification(config, _EVENT, transport=transport)

    payload = json.loads(seen[0].content)
    assert "embeds" in payload
    embed = payload["embeds"][0]
    assert embed["title"] == "Downmix failed"
    assert embed["description"] == "FFmpeg exited non-zero for The Show S01E01"
    assert {"name": "file", "value": "/media/tv/The Show/S01E01.mkv", "inline": True} in embed[
        "fields"
    ]


def test_event_without_details_omits_details_and_fields() -> None:
    import json

    event = NotificationEvent(event_type="health_check_failed", title="FFmpeg missing", message="")
    transport, seen = _ok_transport()
    config = _config(webhook_url="https://example.com/hook", webhook_enabled=True)

    dispatch_notification(config, event, transport=transport)

    payload = json.loads(seen[0].content)
    assert "details" not in payload


# ---------------------------------------------------------------------------
# Misconfiguration: enabled but no URL.
# ---------------------------------------------------------------------------


def test_dispatch_reports_failure_for_enabled_webhook_with_no_url() -> None:
    config = _config(webhook_enabled=True)

    results = dispatch_notification(config, _EVENT, transport=_ok_transport()[0])

    assert len(results) == 1
    assert results[0].notifier == WEBHOOK_NOTIFIER
    assert results[0].ok is False
    assert results[0].error is not None


def test_dispatch_reports_failure_for_enabled_discord_with_no_url() -> None:
    config = _config(discord_enabled=True)

    results = dispatch_notification(config, _EVENT, transport=_ok_transport()[0])

    assert len(results) == 1
    assert results[0].notifier == DISCORD_NOTIFIER
    assert results[0].ok is False


def test_dispatch_no_url_case_makes_no_network_call() -> None:
    transport, seen = _ok_transport()
    config = _config(webhook_enabled=True, webhook_url=None)

    dispatch_notification(config, _EVENT, transport=transport)

    assert len(seen) == 0


# ---------------------------------------------------------------------------
# Network / HTTP failures are isolated per notifier.
# ---------------------------------------------------------------------------


def test_dispatch_reports_http_error_status_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    transport = httpx.MockTransport(handler)
    config = _config(webhook_url="https://example.com/hook", webhook_enabled=True)

    results = dispatch_notification(config, _EVENT, transport=transport)

    assert results[0].ok is False
    assert results[0].error is not None
    assert "500" in results[0].error


def test_dispatch_reports_connection_error_as_failure_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)
    config = _config(webhook_url="https://example.com/hook", webhook_enabled=True)

    results = dispatch_notification(config, _EVENT, transport=transport)

    assert results[0].ok is False
    assert results[0].error is not None


def test_dispatch_isolates_a_failing_notifier_from_a_succeeding_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "discord" in str(request.url):
            raise httpx.ConnectTimeout("Timed out", request=request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    config = _config(
        webhook_url="https://example.com/hook",
        webhook_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/1/abc",
        discord_enabled=True,
    )

    results = dispatch_notification(config, _EVENT, transport=transport)

    by_notifier = {r.notifier: r for r in results}
    assert by_notifier[WEBHOOK_NOTIFIER].ok is True
    assert by_notifier[DISCORD_NOTIFIER].ok is False


@pytest.mark.parametrize("status_code", [400, 401, 404, 503])
def test_dispatch_reports_various_error_statuses_as_failure(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    transport = httpx.MockTransport(handler)
    config = _config(webhook_url="https://example.com/hook", webhook_enabled=True)

    results = dispatch_notification(config, _EVENT, transport=transport)

    assert results[0].ok is False
