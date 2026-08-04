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
    """File info extracted from a webhook payload, before path resolution.

    ``sonarr_episode_id``/``radarr_movie_id`` (COL-101) are the Arr
    instance's own object ids -- read straight off the payload's
    ``episodes``/``movie`` object, matching
    :class:`~collapsarr.arr.files.MonitoredFile`'s scan-path fields of the
    same name. A Sonarr multi-episode release reports more than one entry in
    ``episodes``; only the *first* one's id is kept, same simplification
    :func:`~collapsarr.arr.files._fetch_sonarr_files` makes for the scan path.

    ``sonarr_series_id``/``season_number``/``episode_number``/``episode_title``
    (COL-102) are the remaining Series > Season > Episode catalog fields the
    webhook payload carries -- enough to upsert the corresponding Library
    node(s) in real time on an import event, the same tree the periodic scan
    mirrors (see :func:`collapsarr.library.service.upsert_series_episode_node`).
    All are Sonarr-only and ``None`` on a Radarr file (a Movie node needs only
    its ``radarr_movie_id`` + title). ``media_title`` doubles as the Series
    (Sonarr) or Movie (Radarr) node title.
    """

    media_title: str
    file_path: str
    is_upgrade: bool
    source_file_id: int | None = None
    sonarr_episode_id: int | None = None
    radarr_movie_id: int | None = None
    sonarr_series_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_title: str | None = None


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
    sonarr_episode_id: int | None = None
    radarr_movie_id: int | None = None
    sonarr_series_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_title: str | None = None


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


def _optional_str(container: dict[str, Any], field: str) -> str | None:
    value = container.get(field)
    return value if isinstance(value, str) else None


def _first_episode(payload: dict[str, Any]) -> dict[str, Any]:
    """The first episode object in a Sonarr webhook payload's ``episodes`` array.

    Returns an empty dict when there is no ``episodes`` array (or it has no
    dict entries), so every per-episode field
    (:func:`_optional_int`/:func:`_optional_str`) simply resolves to ``None``
    -- a Download event with no episode data still parses, its ids just stay
    unset. A multi-episode release keeps only the *first* entry, the same
    simplification the scan path makes.
    """
    episodes = payload.get("episodes")
    if not isinstance(episodes, list):
        return {}
    for episode in episodes:
        if isinstance(episode, dict):
            return episode
    return {}


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

    episode = _first_episode(payload)
    return WebhookFile(
        media_title=media_title,
        file_path=file_path,
        is_upgrade=bool(payload.get("isUpgrade", False)),
        source_file_id=_optional_int(episode_file, "id"),
        sonarr_episode_id=_optional_int(episode, "id"),
        sonarr_series_id=_optional_int(series, "id"),
        season_number=_optional_int(episode, "seasonNumber"),
        episode_number=_optional_int(episode, "episodeNumber"),
        episode_title=_optional_str(episode, "title"),
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
        radarr_movie_id=_optional_int(movie, "id"),
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
        sonarr_episode_id=file.sonarr_episode_id,
        radarr_movie_id=file.radarr_movie_id,
        sonarr_series_id=file.sonarr_series_id,
        season_number=file.season_number,
        episode_number=file.episode_number,
        episode_title=file.episode_title,
    )
