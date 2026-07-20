"""Inbound Sonarr/Radarr "on import"/"on upgrade" webhook handling (COL-14).

Sonarr and Radarr both fire the same webhook event -- ``eventType: "Download"``
-- for both a fresh import and a quality upgrade, distinguished only by the
``isUpgrade`` flag on the payload. Other eventTypes (``Test``, ``Grab``,
``Rename``, ``Health``, ...) are accepted and acknowledged with 200 but not
otherwise acted on -- that matches Sonarr/Radarr's own webhook UI, whose
"Test" button expects a 2xx response for any configured event, not just
Download.

Neither Sonarr's nor Radarr's webhook payload carries an instance identifier
of its own, so :mod:`collapsarr.main` routes webhooks per configured instance
(``POST /api/webhook/arr/{instance_id}``) and this module dispatches parsing
on that instance's ``type`` rather than trying to sniff the payload shape.

Path resolution reuses :func:`collapsarr.arr.models.resolve_path` -- the same
instance + path-mapping logic used elsewhere -- so a webhook-reported
container path arrives at the "file ready" hook already translated to a
host-local path.

The "file ready" hook itself is intentionally pluggable: this module ships a
stub/log implementation (:func:`default_on_file_ready_hook`), and the Job
Queue & Scheduler epic (COL-22) wires the real one --
:meth:`collapsarr.jobs.scheduler.JobScheduler.on_file_ready`, which enqueues a
downmix job -- in via ``create_app(enable_scheduler=True)``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import ArrInstance, InstanceType, RemotePathMapping, resolve_path

logger = logging.getLogger(__name__)

# The eventType Sonarr/Radarr fire on both a fresh import and an upgrade;
# distinguished only by the payload's `isUpgrade` flag.
_DOWNLOAD_EVENT = "Download"


class WebhookValidationError(ValueError):
    """Raised when a webhook payload is malformed for its declared event type."""


@dataclass(frozen=True, slots=True)
class WebhookFile:
    """File info extracted from a webhook payload, before path resolution."""

    media_title: str
    file_path: str
    is_upgrade: bool
    source_file_id: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedWebhookFile:
    """A webhook-reported file after instance + path-mapping resolution.

    Normalized the same way :class:`collapsarr.arr.files.MonitoredFile` is,
    so downstream consumers (e.g. the future Job Queue) see one consistent
    shape regardless of whether a file was discovered via polling or a
    webhook.
    """

    instance_id: int
    instance_name: str
    media_title: str
    file_path: str
    is_upgrade: bool
    source_file_id: int | None = None


OnFileReadyHook = Callable[[ResolvedWebhookFile], None]


def default_on_file_ready_hook(file: ResolvedWebhookFile) -> None:
    """Stub "file ready" hook: logs the resolved file.

    The default fallback when no scheduler is wired. In production the real
    hook (:meth:`collapsarr.jobs.scheduler.JobScheduler.on_file_ready`, which
    enqueues a downmix job) is wired in via ``create_app(enable_scheduler=True)``.
    """
    action = "upgraded" if file.is_upgrade else "imported"
    logger.info(
        "arr webhook: %s ready (%s) from instance %r (id=%s): %s",
        action,
        file.media_title,
        file.instance_name,
        file.instance_id,
        file.file_path,
    )


def _require_dict(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WebhookValidationError(f"Missing or invalid '{field}' object in webhook payload")
    return value


def _require_str(container: dict[str, Any], field: str) -> str:
    value = container.get(field)
    if not isinstance(value, str) or not value:
        raise WebhookValidationError(f"Missing or invalid '{field}' field in webhook payload")
    return value


def _optional_int(container: dict[str, Any], field: str) -> int | None:
    value = container.get(field)
    return value if isinstance(value, int) else None


def parse_sonarr_webhook(payload: dict[str, Any]) -> WebhookFile | None:
    """Parse a Sonarr webhook payload into a :class:`WebhookFile`.

    Returns ``None`` for a valid payload whose ``eventType`` is not
    ``"Download"`` (import/upgrade) -- those are acknowledged but not acted
    on. Raises :class:`WebhookValidationError` if a ``Download`` event is
    missing the ``series``/``episodeFile`` data needed to resolve a file.
    """
    event_type = payload.get("eventType")
    if not isinstance(event_type, str) or not event_type:
        raise WebhookValidationError("Missing or invalid 'eventType' field in webhook payload")
    if event_type != _DOWNLOAD_EVENT:
        return None

    series = _require_dict(payload.get("series"), "series")
    media_title = _require_str(series, "title")

    episode_file = _require_dict(payload.get("episodeFile"), "episodeFile")
    file_path = _require_str(episode_file, "path")

    return WebhookFile(
        media_title=media_title,
        file_path=file_path,
        is_upgrade=bool(payload.get("isUpgrade", False)),
        source_file_id=_optional_int(episode_file, "id"),
    )


def parse_radarr_webhook(payload: dict[str, Any]) -> WebhookFile | None:
    """Parse a Radarr webhook payload into a :class:`WebhookFile`.

    Mirror of :func:`parse_sonarr_webhook` for Radarr's ``movie``/``movieFile``
    shape.
    """
    event_type = payload.get("eventType")
    if not isinstance(event_type, str) or not event_type:
        raise WebhookValidationError("Missing or invalid 'eventType' field in webhook payload")
    if event_type != _DOWNLOAD_EVENT:
        return None

    movie = _require_dict(payload.get("movie"), "movie")
    media_title = _require_str(movie, "title")

    movie_file = _require_dict(payload.get("movieFile"), "movieFile")
    file_path = _require_str(movie_file, "path")

    return WebhookFile(
        media_title=media_title,
        file_path=file_path,
        is_upgrade=bool(payload.get("isUpgrade", False)),
        source_file_id=_optional_int(movie_file, "id"),
    )


def parse_webhook_payload(
    instance_type: InstanceType, payload: dict[str, Any]
) -> WebhookFile | None:
    """Dispatch webhook parsing based on the target instance's configured type.

    The payload itself carries no instance identifier or explicit
    "this is Sonarr/Radarr" marker, so which parser to use is decided by
    which :class:`~collapsarr.arr.models.ArrInstance` the webhook URL names
    (looked up by id), not by sniffing the payload shape.
    """
    if instance_type is InstanceType.SONARR:
        return parse_sonarr_webhook(payload)
    if instance_type is InstanceType.RADARR:
        return parse_radarr_webhook(payload)
    msg = f"Unsupported instance type: {instance_type!r}"
    raise WebhookValidationError(msg)  # pragma: no cover


def resolve_webhook_file(
    instance: ArrInstance,
    file: WebhookFile,
    mappings: list[RemotePathMapping] | None = None,
) -> ResolvedWebhookFile:
    """Apply instance + path-mapping resolution to a parsed webhook file."""
    return ResolvedWebhookFile(
        instance_id=instance.id,
        instance_name=instance.name,
        media_title=file.media_title,
        file_path=resolve_path(file.file_path, mappings),
        is_upgrade=file.is_upgrade,
        source_file_id=file.source_file_id,
    )
