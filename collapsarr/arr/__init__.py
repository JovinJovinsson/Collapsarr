"""Sonarr/Radarr instance connection management (COL-11).

Home for arr-integration concerns per ``docs/TRACKER.md``: instance config
model, connectivity/version checks, and (in later tickets) API polling,
webhooks, and remote path mapping.

This module is imported for its side effect of registering
:class:`~collapsarr.arr.models.ArrInstance` with
:data:`collapsarr.database.Base.metadata` — see
:func:`collapsarr.database.init_db`.
"""

from __future__ import annotations

from .client import ConnectivityResult, check_connectivity
from .models import ArrInstance, ConnectivityStatus, InstanceType
from .service import (
    InstanceNotFoundError,
    create_instance,
    delete_instance,
    get_instance,
    list_instances,
    update_instance,
)

__all__ = [
    "ArrInstance",
    "ConnectivityResult",
    "ConnectivityStatus",
    "InstanceNotFoundError",
    "InstanceType",
    "check_connectivity",
    "create_instance",
    "delete_instance",
    "get_instance",
    "list_instances",
    "update_instance",
]
