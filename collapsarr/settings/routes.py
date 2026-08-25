"""HTTP REST endpoints for global Settings (COL-28).

Thin GET/PUT layer over :mod:`collapsarr.settings.service`, exposed as a
FastAPI :class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Because everything under ``/api`` is gated
by the API-key middleware (COL-26), both routes here inherit key-based auth --
no per-route auth wiring is needed.

The persisted :class:`~collapsarr.settings.models.GlobalSettings` row stores
``enabled_targets``/``language_allow_list`` as comma-joined strings; the REST
surface exposes them as proper JSON arrays (decoded via
:func:`~collapsarr.settings.service.as_downmix_settings`) so the API round-trips
cleanly. ``api_key`` is surfaced read-only (mirroring how Sonarr/Radarr's
``config/host`` exposes ``apiKey``) -- it is minted automatically and rotated
through a dedicated flow, never set via this body.

``PUT /api/settings`` follows the same partial-update convention as
:func:`collapsarr.arr.routes.update_instance_endpoint`: only fields present in
the request body are changed. For the three fields whose valid domain includes
``null`` (``language_allow_list``, ``stereo_bitrate_kbps``,
``surround_bitrate_kbps``), sending an explicit ``null`` *clears* the stored
value, while omitting the field leaves it untouched -- the distinction is drawn
with Pydantic's ``model_fields_set`` so "omitted" and "explicitly null" never
collapse together.

``auth_required`` (COL-51) is also read/write here -- ``"local_bypass"``
(default) vs. ``"enabled"`` -- so Settings can flip an install's required-mode;
see :mod:`collapsarr.auth.enforcement` for what each mode does.

``auth_method`` (COL-52) is read/write the same way -- ``"forms"`` (default,
the sign-in page) vs. ``"basic"`` (a browser's native HTTP Basic prompt) --
so Settings can switch how the same credential (COL-49) is presented; see
:mod:`collapsarr.auth.enforcement` for how each method challenges a request.

``backup_interval_days``/``backup_retention_days`` (COL-66) are also
read/write here -- the two knobs the Backups page's inline controls persist.
Both are constrained to positive integers (``Field(gt=0)``, matching the
"sensible validation" acceptance criterion): a non-positive value is rejected
with a ``422`` before it ever reaches the service layer. Neither has a
dedicated endpoint -- they round-trip through the same ``GET``/``PUT
/api/settings`` as every other setting. The values are inert here: COL-67's
scheduler is what reads the interval to decide when to run, and COL-68's
pruning is what reads the retention to decide what to delete.

``disk_space_warning_percent``/``disk_space_error_percent`` (COL-79) are the
two free-space-percentage thresholds the disk-space health check
(:mod:`collapsarr.health.disk_space`) reads live on every scheduler tick --
same round-trip-only-through-Settings treatment as the backup pair, each
constrained to ``(0, 100]`` (``Field(gt=0, le=100)``) since a percentage
outside that range can never be crossed by a real free-space reading.

``update_channel`` (COL-88) is also read/write here -- ``"stable"`` (default)
vs. ``"beta"`` -- selecting which GitHub Release stream the Update Check
(:mod:`collapsarr.update_check`) compares the running instance against. Typed
as a ``Literal`` the same way as ``auth_required``/``auth_method`` so an
unrecognised value is rejected with a ``422`` before it reaches the service
layer, which validates it again independently (see
:func:`collapsarr.settings.service.update_global_settings`) for callers that
bypass this HTTP layer.

``log_level`` (COL-130) is also read/write here -- ``DEBUG``/``INFO``/
``WARNING``/``ERROR``, or ``null`` -- the runtime override for the
``collapsarr`` logger surfaced in Settings -> General's log-level dropdown.
Like ``language_allow_list``, ``null`` is a meaningful, sendable value (clears
a persisted override back to deferring to ``COLLAPSARR_LOG_LEVEL``); omitting
the field leaves it untouched. Unlike every other field here,
``update_settings_endpoint`` calls
:func:`collapsarr.logging_setup.apply_log_level` after persisting it, so the
change is live on the ``collapsarr`` logger immediately, not just on the
Update Check scheduler's/disk-space check's next tick.

``default_audio_language``/``default_audio_channel_tier``/
``auto_set_default_audio`` (COL-151) are also read/write here -- the
Preferred Default Audio setting a later ticket's Targets settings page
(COL-159) will surface. ``default_audio_channel_tier`` reuses
:class:`~collapsarr.downmix.targets.DownmixTarget` the same way
``enabled_targets`` already does, so an unrecognised tier is rejected with a
``422`` the same way an unrecognised downmix target already is. Like
``language_allow_list``/``log_level``, ``null`` is a meaningful, sendable
value for the two nullable fields (clears a configured preference); omitting
either field leaves it untouched. ``auto_set_default_audio`` is a plain
boolean, same treatment as ``ui_auth_enabled``/``default_tracked``.

``default_audio_delay_minutes`` (COL-243) is also read/write here -- the
minimum age (in minutes) a remuxed file's new audio streams must have
before Collapsarr trusts Plex to have already processed them for a Default
Audio Track write/verify. Constrained to ``>= 0`` (``Field(ge=0)``), same
treatment as ``recently_processed_window_minutes`` below. No dedicated
endpoint: it round-trips through this same ``GET``/``PUT /api/settings``.
Not yet read by any Job-scheduling logic -- COL-251 is the follow-up
ticket that will consume it.

``recently_processed_window_minutes`` (COL-167) is also read/write here --
the scheduler's "recently processed" dedup cooldown, in minutes, no longer
silently derived from ``scan_interval_hours``. Constrained to ``>= 0``
(``Field(ge=0)``, unlike the backup/disk-space knobs above, since ``0`` is a
valid, meaningful value here -- "no cooldown", not "unset"). No dedicated
endpoint: it round-trips through this same ``GET``/``PUT /api/settings``
alongside ``concurrency_limit``. Unlike ``concurrency_limit`` (read once at
worker-pool construction), :class:`~collapsarr.jobs.scheduler.JobScheduler`
reads this field live from the settings row on every dedup check, so a
``PUT`` here changes behavior on the very next check with no restart.

``auto_queue_paused`` (COL-174, "Auto-Queuing Pause") is also read/write
here -- a plain boolean, same treatment as ``ui_auth_enabled``/
``default_tracked``/``auto_set_default_audio``, no dedicated endpoint. When
set, the scanner's Wanted-driven auto-fill (the periodic scan's initial
enqueue and the Auto-Queue Limit's completion/cancellation-triggered
top-up) stops running; already-``PENDING``/``RUNNING`` Jobs and every manual
trigger (single-file trigger, single/bulk requeue) are unaffected -- see
:class:`~collapsarr.settings.models.GlobalSettings`'s own docstring for the
full scope. :class:`~collapsarr.jobs.scheduler.JobScheduler` reads it live
on every top-up attempt, so a ``PUT`` here takes effect immediately with no
restart.

``auto_processing_paused`` (COL-226, "Auto-Processing Pause") is also
read/write here -- a plain boolean, same treatment as ``auto_queue_paused``
above, no dedicated endpoint. When set, :class:`~collapsarr.jobs.queue.
JobQueue` stops claiming any new ``PENDING`` Job -- already-``RUNNING`` Jobs
keep running to completion. Deliberately distinct from ``auto_queue_paused``:
that field only stops the scanner from *adding* new pending Jobs, while this
one stops already-pending Jobs (however they got there) from ever starting --
see :class:`~collapsarr.settings.models.GlobalSettings`'s own docstring for
the full scope. ``JobQueue`` reads it live on every claim attempt via its
injected ``pause_check`` callable, so a ``PUT`` here takes effect on the very
next claim with no restart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..database import get_session
from ..downmix.targets import DownmixTarget
from ..logging_setup import apply_log_level
from .models import GlobalSettings
from .service import as_downmix_settings, get_global_settings, update_global_settings

AuthRequiredMode = Literal["enabled", "local_bypass"]
"""The two required-modes settable from Settings (COL-51), matching
:data:`collapsarr.settings.models.AUTH_REQUIRED_ENABLED` /
:data:`~collapsarr.settings.models.AUTH_REQUIRED_LOCAL_BYPASS` -- spelled out
as literals (not the constants themselves) so mypy accepts them in a
``Literal[...]``."""

AuthMethodMode = Literal["forms", "basic"]
"""The two auth methods settable from Settings (COL-52), matching
:data:`collapsarr.settings.models.AUTH_METHOD_FORMS` /
:data:`~collapsarr.settings.models.AUTH_METHOD_BASIC` -- spelled out as
literals for the same reason as :data:`AuthRequiredMode`."""

UpdateChannelMode = Literal["stable", "beta"]
"""The two release channels settable from Settings (COL-88), matching
:data:`collapsarr.settings.models.UPDATE_CHANNEL_STABLE` /
:data:`~collapsarr.settings.models.UPDATE_CHANNEL_BETA` -- spelled out as
literals for the same reason as :data:`AuthRequiredMode`."""

LogLevelMode = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
"""The four levels settable from Settings -> General's log-level dropdown
(COL-130), matching :data:`collapsarr.settings.models.LOG_LEVELS` -- spelled
out as literals for the same reason as :data:`AuthRequiredMode`."""

router = APIRouter(prefix="/api", tags=["settings"])


# --- schemas -----------------------------------------------------------------


class SettingsRead(BaseModel):
    """Response shape for the global settings row.

    ``enabled_targets``/``language_allow_list`` are decoded from their stored
    comma-joined form into JSON arrays; ``api_key`` is read-only.
    """

    enabled_targets: list[DownmixTarget]
    language_allow_list: list[str] | None
    stereo_codec: str
    stereo_bitrate_kbps: int | None
    surround_codec: str
    surround_bitrate_kbps: int | None
    concurrency_limit: int
    ui_auth_enabled: bool
    auth_required: AuthRequiredMode
    auth_method: AuthMethodMode
    backup_interval_days: int
    backup_retention_days: int
    disk_space_warning_percent: float
    disk_space_error_percent: float
    update_channel: UpdateChannelMode
    default_tracked: bool
    log_level: LogLevelMode | None
    default_audio_language: str | None
    default_audio_channel_tier: DownmixTarget | None
    auto_set_default_audio: bool
    default_audio_delay_minutes: int
    recently_processed_window_minutes: int
    auto_queue_paused: bool
    auto_processing_paused: bool
    api_key: str
    created_at: datetime
    updated_at: datetime


class SettingsUpdate(BaseModel):
    """Request body to update global settings; omitted fields are left unchanged.

    ``enabled_targets`` and ``language_allow_list`` accept JSON arrays. Sending
    an explicit ``null`` for ``language_allow_list``/``stereo_bitrate_kbps``/
    ``surround_bitrate_kbps``/``log_level`` clears the stored override;
    omitting the field leaves it untouched.
    """

    model_config = ConfigDict(extra="forbid")

    enabled_targets: list[DownmixTarget] | None = None
    language_allow_list: list[str] | None = None
    stereo_codec: str | None = None
    stereo_bitrate_kbps: int | None = None
    surround_codec: str | None = None
    surround_bitrate_kbps: int | None = None
    concurrency_limit: int | None = None
    ui_auth_enabled: bool | None = None
    auth_required: AuthRequiredMode | None = None
    auth_method: AuthMethodMode | None = None
    backup_interval_days: int | None = Field(default=None, gt=0)
    backup_retention_days: int | None = Field(default=None, gt=0)
    disk_space_warning_percent: float | None = Field(default=None, gt=0, le=100)
    disk_space_error_percent: float | None = Field(default=None, gt=0, le=100)
    update_channel: UpdateChannelMode | None = None
    default_tracked: bool | None = None
    log_level: LogLevelMode | None = None
    default_audio_language: str | None = None
    default_audio_channel_tier: DownmixTarget | None = None
    auto_set_default_audio: bool | None = None
    default_audio_delay_minutes: int | None = Field(default=None, ge=0)
    recently_processed_window_minutes: int | None = Field(default=None, ge=0)
    auto_queue_paused: bool | None = None
    auto_processing_paused: bool | None = None


def _to_read(settings: GlobalSettings) -> SettingsRead:
    """Adapt a persisted settings row into its JSON response shape.

    Reuses :func:`~collapsarr.settings.service.as_downmix_settings` to decode
    the comma-joined ``enabled_targets``/``language_allow_list`` columns so the
    decode logic lives in exactly one place.
    """
    downmix = as_downmix_settings(settings)
    return SettingsRead(
        enabled_targets=sorted(downmix.enabled_targets, key=lambda target: target.value),
        language_allow_list=(
            sorted(downmix.language_allow_list)
            if downmix.language_allow_list is not None
            else None
        ),
        stereo_codec=settings.stereo_codec,
        stereo_bitrate_kbps=settings.stereo_bitrate_kbps,
        surround_codec=settings.surround_codec,
        surround_bitrate_kbps=settings.surround_bitrate_kbps,
        concurrency_limit=settings.concurrency_limit,
        ui_auth_enabled=settings.ui_auth_enabled,
        auth_required=settings.auth_required,
        auth_method=settings.auth_method,
        backup_interval_days=settings.backup_interval_days,
        backup_retention_days=settings.backup_retention_days,
        disk_space_warning_percent=settings.disk_space_warning_percent,
        disk_space_error_percent=settings.disk_space_error_percent,
        update_channel=settings.update_channel,
        default_tracked=settings.default_tracked,
        log_level=settings.log_level,
        default_audio_language=settings.default_audio_language,
        default_audio_channel_tier=(
            DownmixTarget(settings.default_audio_channel_tier)
            if settings.default_audio_channel_tier is not None
            else None
        ),
        auto_set_default_audio=settings.auto_set_default_audio,
        default_audio_delay_minutes=settings.default_audio_delay_minutes,
        recently_processed_window_minutes=settings.recently_processed_window_minutes,
        auto_queue_paused=settings.auto_queue_paused,
        auto_processing_paused=settings.auto_processing_paused,
        api_key=settings.api_key,
        created_at=settings.created_at,
        updated_at=settings.updated_at,
    )


# --- endpoints ---------------------------------------------------------------


@router.get("/settings", response_model=SettingsRead)
def get_settings_endpoint(session: Session = Depends(get_session)) -> SettingsRead:
    """Return the global settings row, creating it with defaults on first read."""
    return _to_read(get_global_settings(session))


@router.put("/settings", response_model=SettingsRead)
def update_settings_endpoint(
    body: SettingsUpdate, session: Session = Depends(get_session)
) -> SettingsRead:
    """Update the provided settings fields and return the full row.

    Only fields present in the request body are changed; the nullable-domain
    fields treat an explicit ``null`` as "clear this override". A provided
    ``log_level`` is additionally applied live to the running ``collapsarr``
    logger (:func:`collapsarr.logging_setup.apply_log_level`) once persisted.
    """
    provided = body.model_fields_set
    kwargs: dict[str, object] = {}

    if "enabled_targets" in provided:
        targets = body.enabled_targets or []
        kwargs["enabled_targets"] = frozenset(targets)
    if "language_allow_list" in provided:
        kwargs["language_allow_list"] = (
            frozenset(body.language_allow_list)
            if body.language_allow_list is not None
            else None
        )
    if "stereo_codec" in provided:
        kwargs["stereo_codec"] = body.stereo_codec
    if "stereo_bitrate_kbps" in provided:
        kwargs["stereo_bitrate_kbps"] = body.stereo_bitrate_kbps
    if "surround_codec" in provided:
        kwargs["surround_codec"] = body.surround_codec
    if "surround_bitrate_kbps" in provided:
        kwargs["surround_bitrate_kbps"] = body.surround_bitrate_kbps
    if "concurrency_limit" in provided:
        kwargs["concurrency_limit"] = body.concurrency_limit
    if "ui_auth_enabled" in provided:
        kwargs["ui_auth_enabled"] = body.ui_auth_enabled
    if "auth_required" in provided:
        kwargs["auth_required"] = body.auth_required
    if "auth_method" in provided:
        kwargs["auth_method"] = body.auth_method
    if "backup_interval_days" in provided:
        kwargs["backup_interval_days"] = body.backup_interval_days
    if "backup_retention_days" in provided:
        kwargs["backup_retention_days"] = body.backup_retention_days
    if "disk_space_warning_percent" in provided:
        kwargs["disk_space_warning_percent"] = body.disk_space_warning_percent
    if "disk_space_error_percent" in provided:
        kwargs["disk_space_error_percent"] = body.disk_space_error_percent
    if "update_channel" in provided:
        kwargs["update_channel"] = body.update_channel
    if "default_tracked" in provided:
        kwargs["default_tracked"] = body.default_tracked
    if "log_level" in provided:
        kwargs["log_level"] = body.log_level
    if "default_audio_language" in provided:
        kwargs["default_audio_language"] = body.default_audio_language
    if "default_audio_channel_tier" in provided:
        kwargs["default_audio_channel_tier"] = body.default_audio_channel_tier
    if "auto_set_default_audio" in provided:
        kwargs["auto_set_default_audio"] = body.auto_set_default_audio
    if "default_audio_delay_minutes" in provided:
        kwargs["default_audio_delay_minutes"] = body.default_audio_delay_minutes
    if "recently_processed_window_minutes" in provided:
        kwargs["recently_processed_window_minutes"] = body.recently_processed_window_minutes
    if "auto_queue_paused" in provided:
        kwargs["auto_queue_paused"] = body.auto_queue_paused
    if "auto_processing_paused" in provided:
        kwargs["auto_processing_paused"] = body.auto_processing_paused

    updated = update_global_settings(session, **kwargs)  # type: ignore[arg-type]
    if "log_level" in provided:
        apply_log_level(updated.log_level)
    return _to_read(updated)
