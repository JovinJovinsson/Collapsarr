"""Global application settings: persistence and read/write service (COL-24).

Home for settings concerns per ``docs/TRACKER.md``'s "Settings & Persistence"
component: a single, persisted Settings row (enabled downmix targets,
language allow-list, codec/bitrate overrides, concurrency limit, UI auth
toggle) with documented defaults on first run, and the service-layer
interface other epics (Job Queue & Scheduler, Downmix Engine, and eventually
the Web UI) read from. Notification config is a separate ticket's (COL-35)
concern and is not part of this module.

This module is imported for its side effect of registering
:class:`~collapsarr.settings.models.GlobalSettings` with
:data:`collapsarr.database.Base.metadata` -- see
:func:`collapsarr.database.init_db`.
"""

from __future__ import annotations

from .models import SETTINGS_ID, GlobalSettings, generate_api_key
from .service import as_downmix_settings, get_global_settings, update_global_settings

__all__ = [
    "SETTINGS_ID",
    "GlobalSettings",
    "as_downmix_settings",
    "generate_api_key",
    "get_global_settings",
    "update_global_settings",
]
