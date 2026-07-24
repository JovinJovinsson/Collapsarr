"""Environment-seeded UI credential for headless deploys (COL-53).

A declarative/headless deploy (Docker, an automated provisioning tool) has no
human available to click through the interactive first-run ``/setup`` gate
(COL-50). This module is that escape hatch: if
``COLLAPSARR_AUTH_USERNAME``/``COLLAPSARR_AUTH_PASSWORD`` (see
:mod:`collapsarr.config`) are set, :func:`seed_auth_from_env` persists that
credential -- hashed via :mod:`collapsarr.settings.passwords` (COL-49), never
in plaintext -- the first time the app boots with no credential configured
yet, so the instance comes up already past ``/setup``. The optional
``COLLAPSARR_AUTH_METHOD``/``COLLAPSARR_AUTH_REQUIRED`` variables are applied
at the same time, when present; otherwise the seeded row keeps
:class:`~collapsarr.settings.models.GlobalSettings`'s own defaults (forms,
local_bypass).

Seeding is **one-shot**: once a credential exists (``auth_username`` is set --
the same check :mod:`collapsarr.auth.enforcement` and
:mod:`collapsarr.auth.routes` use for "is setup done"), this is a no-op on
every later boot, even if the environment variables are still present. That
is what makes the variables safe to leave in a compose file/systemd unit
permanently, and it is also the documented password-recovery/lockout path: an
operator locked out of a *fresh* install (nothing set yet) can set these and
restart to get back in, but they do **not** help recover a *forgotten*
password once a credential already exists -- clearing the existing
``auth_username``/``auth_password_hash`` first (e.g. directly in the
database) is what that would take.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import Settings
from .models import GlobalSettings
from .service import get_global_settings, update_global_settings


def seed_auth_from_env(session: Session, settings: Settings) -> GlobalSettings | None:
    """Seed the UI credential from the environment on first boot, if needed.

    Returns the updated :class:`~collapsarr.settings.models.GlobalSettings`
    row when a credential was actually seeded, or ``None`` when nothing was
    done -- either because ``settings.auth_username``/``auth_password``
    aren't both set, or because a credential already exists (idempotent:
    never overwrites an existing, possibly since-changed credential).
    """
    row = get_global_settings(session)
    if row.auth_username is not None:
        return None
    if not settings.auth_username or not settings.auth_password:
        return None

    return update_global_settings(
        session,
        auth_username=settings.auth_username,
        password=settings.auth_password,
        auth_method=settings.auth_method,
        auth_required=settings.auth_required,
    )
