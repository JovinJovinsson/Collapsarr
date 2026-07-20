"""Fan-out dispatch of a notification event to all enabled notifiers (COL-35).

:func:`dispatch_notification` takes a generic :class:`NotificationEvent` --
the shape the Connect & Notifications epic's later tickets (COL-37 "Notify on
downmix failure", COL-38 "Notify on app health issues") will construct -- and
POSTs it to every notifier enabled on a :class:`~collapsarr.notify.models.
NotifierConfig` row: a generic JSON webhook and/or a Discord webhook (using
Discord's ``embeds`` shape so messages render nicely in a Discord channel).

Mirrors :mod:`collapsarr.arr.client`'s conventions: never raises -- network
errors, timeouts, and non-2xx responses are all captured as a failed
:class:`NotifierDispatchResult` per notifier, so one notifier failing never
blocks another, and tests inject a ``transport`` (``httpx.MockTransport``)
instead of making real network calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from .models import NotifierConfig

_DEFAULT_TIMEOUT = 10.0
_ERROR_BODY_LIMIT = 500

WEBHOOK_NOTIFIER = "webhook"
DISCORD_NOTIFIER = "discord"


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """A single event to notify about, independent of which notifier sends it.

    ``event_type`` is a short machine-readable tag (e.g. ``"downmix_failure"``,
    ``"health_check_failed"``); ``title``/``message`` are the human-readable
    summary/body. ``details`` is optional free-form key/value context (e.g.
    a file path or error string) rendered as extra fields where the notifier
    supports it.
    """

    event_type: str
    title: str
    message: str
    details: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class NotifierDispatchResult:
    """Outcome of sending a :class:`NotificationEvent` to one notifier."""

    notifier: str
    ok: bool
    error: str | None = None


def _generic_webhook_payload(event: NotificationEvent) -> dict[str, object]:
    """Build the JSON body posted to the generic webhook notifier."""
    payload: dict[str, object] = {
        "event_type": event.event_type,
        "title": event.title,
        "message": event.message,
    }
    if event.details:
        payload["details"] = dict(event.details)
    return payload


def _discord_webhook_payload(event: NotificationEvent) -> dict[str, object]:
    """Build the JSON body posted to Discord, using its ``embeds`` shape."""
    embed: dict[str, object] = {"title": event.title, "description": event.message}
    if event.details:
        embed["fields"] = [
            {"name": key, "value": value, "inline": True} for key, value in event.details.items()
        ]
    return {"embeds": [embed]}


def _post(
    client: httpx.Client, notifier: str, url: str, payload: dict[str, object]
) -> NotifierDispatchResult:
    """POST ``payload`` to ``url``, reporting success/failure without raising."""
    try:
        response = client.post(url, json=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text}"[:_ERROR_BODY_LIMIT]
        return NotifierDispatchResult(notifier=notifier, ok=False, error=detail)
    except httpx.HTTPError as exc:
        return NotifierDispatchResult(notifier=notifier, ok=False, error=str(exc))
    return NotifierDispatchResult(notifier=notifier, ok=True)


def dispatch_notification(
    config: NotifierConfig,
    event: NotificationEvent,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> list[NotifierDispatchResult]:
    """Send ``event`` to every notifier enabled on ``config``.

    Returns one :class:`NotifierDispatchResult` per notifier that is enabled
    -- a disabled notifier is skipped entirely (no entry in the result). A
    notifier that is enabled but has no URL configured yet is also skipped,
    but reported as a failed result (``error="... not configured"``) rather
    than silently dropped, since that state is a misconfiguration worth
    surfacing. Never raises: per-notifier network failures are isolated so one
    failing notifier doesn't stop the others from being attempted.
    """
    results: list[NotifierDispatchResult] = []

    if not config.webhook_enabled and not config.discord_enabled:
        return results

    client = (
        httpx.Client(timeout=timeout, transport=transport)
        if transport is not None
        else httpx.Client(timeout=timeout)
    )
    with client:
        if config.webhook_enabled:
            if not config.webhook_url:
                results.append(
                    NotifierDispatchResult(
                        notifier=WEBHOOK_NOTIFIER,
                        ok=False,
                        error="Webhook notifier is enabled but has no URL configured",
                    )
                )
            else:
                results.append(
                    _post(
                        client,
                        WEBHOOK_NOTIFIER,
                        config.webhook_url,
                        _generic_webhook_payload(event),
                    )
                )

        if config.discord_enabled:
            if not config.discord_webhook_url:
                results.append(
                    NotifierDispatchResult(
                        notifier=DISCORD_NOTIFIER,
                        ok=False,
                        error="Discord notifier is enabled but has no URL configured",
                    )
                )
            else:
                results.append(
                    _post(
                        client,
                        DISCORD_NOTIFIER,
                        config.discord_webhook_url,
                        _discord_webhook_payload(event),
                    )
                )

    return results
