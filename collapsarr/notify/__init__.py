"""Connect & Notifications: notifier config storage and dispatch (COL-35).

Home for this epic's storage + dispatch concerns per ``docs/TRACKER.md``'s
"Connect & Notifications" component: a single, persisted notifier config row
(generic webhook + Discord, each with a URL and an enabled flag) and a
dispatch service that fans a given event out to every enabled notifier.
Exposing this over HTTP/UI is COL-36's concern and lives elsewhere.

This module is imported for its side effect of registering
:class:`~collapsarr.notify.models.NotifierConfig` with
:data:`collapsarr.database.Base.metadata` -- see
:func:`collapsarr.database.init_db`.
"""

from __future__ import annotations

from .dispatch import (
    DISCORD_NOTIFIER,
    WEBHOOK_NOTIFIER,
    NotificationEvent,
    NotifierDispatchResult,
    dispatch_notification,
)
from .models import NOTIFIER_CONFIG_ID, NotifierConfig
from .service import get_notifier_config, update_notifier_config

__all__ = [
    "DISCORD_NOTIFIER",
    "NOTIFIER_CONFIG_ID",
    "WEBHOOK_NOTIFIER",
    "NotificationEvent",
    "NotifierConfig",
    "NotifierDispatchResult",
    "dispatch_notification",
    "get_notifier_config",
    "update_notifier_config",
]
