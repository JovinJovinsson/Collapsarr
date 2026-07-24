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
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from ..downmix.targets import DownmixTarget
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
    api_key: str
    created_at: datetime
    updated_at: datetime


class SettingsUpdate(BaseModel):
    """Request body to update global settings; omitted fields are left unchanged.

    ``enabled_targets`` and ``language_allow_list`` accept JSON arrays. Sending
    an explicit ``null`` for ``language_allow_list``/``stereo_bitrate_kbps``/
    ``surround_bitrate_kbps`` clears the stored override; omitting the field
    leaves it untouched.
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

    Only fields present in the request body are changed; the three
    nullable-domain fields treat an explicit ``null`` as "clear this override".
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

    return _to_read(update_global_settings(session, **kwargs))  # type: ignore[arg-type]
