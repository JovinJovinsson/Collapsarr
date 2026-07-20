"""HTTP REST endpoints for notifier config (COL-36).

Thin GET/PUT layer over :mod:`collapsarr.notify.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`, matching :mod:`collapsarr.settings.routes`'s
pattern for the global-settings singleton. Because everything under ``/api``
is gated by the API-key middleware (COL-26), both routes here inherit
key-based auth -- no per-route auth wiring is needed.

``PUT /api/notifiers`` follows the same partial-update convention as
:func:`collapsarr.settings.routes.update_settings_endpoint`: only fields
present in the request body are changed. For the two URL fields
(``webhook_url``, ``discord_webhook_url``), sending an explicit ``null``
*clears* the stored URL, while omitting the field leaves it untouched -- the
distinction is drawn with Pydantic's ``model_fields_set`` so "omitted" and
"explicitly null" never collapse together.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .models import NotifierConfig
from .service import get_notifier_config, update_notifier_config

router = APIRouter(prefix="/api", tags=["notifiers"])


# --- schemas -----------------------------------------------------------------


class NotifierConfigRead(BaseModel):
    """Response shape for the notifier config row."""

    webhook_url: str | None
    webhook_enabled: bool
    discord_webhook_url: str | None
    discord_enabled: bool
    created_at: datetime
    updated_at: datetime


class NotifierConfigUpdate(BaseModel):
    """Request body to update notifier config; omitted fields are left unchanged.

    Sending an explicit ``null`` for ``webhook_url``/``discord_webhook_url``
    clears the stored URL; omitting the field leaves it untouched.
    """

    model_config = ConfigDict(extra="forbid")

    webhook_url: str | None = None
    webhook_enabled: bool | None = None
    discord_webhook_url: str | None = None
    discord_enabled: bool | None = None


def _to_read(config: NotifierConfig) -> NotifierConfigRead:
    """Adapt a persisted :class:`NotifierConfig` row into its JSON response shape."""
    return NotifierConfigRead(
        webhook_url=config.webhook_url,
        webhook_enabled=config.webhook_enabled,
        discord_webhook_url=config.discord_webhook_url,
        discord_enabled=config.discord_enabled,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


# --- endpoints ---------------------------------------------------------------


@router.get("/notifiers", response_model=NotifierConfigRead)
def get_notifiers_endpoint(session: Session = Depends(get_session)) -> NotifierConfigRead:
    """Return the notifier config row, creating it with defaults on first read."""
    return _to_read(get_notifier_config(session))


@router.put("/notifiers", response_model=NotifierConfigRead)
def update_notifiers_endpoint(
    body: NotifierConfigUpdate, session: Session = Depends(get_session)
) -> NotifierConfigRead:
    """Update the provided notifier config fields and return the full row.

    Only fields present in the request body are changed; the two URL fields
    treat an explicit ``null`` as "clear this URL".
    """
    provided = body.model_fields_set
    kwargs: dict[str, object] = {}

    if "webhook_url" in provided:
        kwargs["webhook_url"] = body.webhook_url
    if "webhook_enabled" in provided:
        kwargs["webhook_enabled"] = body.webhook_enabled
    if "discord_webhook_url" in provided:
        kwargs["discord_webhook_url"] = body.discord_webhook_url
    if "discord_enabled" in provided:
        kwargs["discord_enabled"] = body.discord_enabled

    return _to_read(update_notifier_config(session, **kwargs))  # type: ignore[arg-type]
