"""Sonarr/Radarr instance connection management (COL-11, COL-12, COL-13, COL-14).

Home for arr-integration concerns per ``docs/TRACKER.md``: instance config
model, connectivity/version checks, the monitored-file-list client, remote
path mapping, and the inbound import/upgrade webhook handler.

This module is imported for its side effect of registering
:class:`~collapsarr.arr.models.ArrInstance` with
:data:`collapsarr.database.Base.metadata` — see
:func:`collapsarr.database.init_db`.
"""

from __future__ import annotations

from .client import ConnectivityResult, check_connectivity
from .files import AudioInfo, MonitoredFile, fetch_monitored_files
from .models import ArrInstance, ConnectivityStatus, InstanceType, RemotePathMapping, resolve_path
from .service import (
    InstanceNotFoundError,
    PathMappingNotFoundError,
    create_instance,
    create_path_mapping,
    delete_instance,
    delete_path_mapping,
    get_instance,
    get_path_mapping,
    list_instances,
    list_path_mappings,
    update_instance,
    update_path_mapping,
)
from .webhooks import (
    OnFileReadyHook,
    ResolvedWebhookFile,
    WebhookFile,
    WebhookValidationError,
    default_on_file_ready_hook,
    parse_radarr_webhook,
    parse_sonarr_webhook,
    parse_webhook_payload,
    resolve_webhook_file,
)

__all__ = [
    "ArrInstance",
    "AudioInfo",
    "ConnectivityResult",
    "ConnectivityStatus",
    "InstanceNotFoundError",
    "InstanceType",
    "MonitoredFile",
    "OnFileReadyHook",
    "PathMappingNotFoundError",
    "RemotePathMapping",
    "ResolvedWebhookFile",
    "WebhookFile",
    "WebhookValidationError",
    "check_connectivity",
    "create_instance",
    "create_path_mapping",
    "default_on_file_ready_hook",
    "delete_instance",
    "delete_path_mapping",
    "fetch_monitored_files",
    "get_instance",
    "get_path_mapping",
    "list_instances",
    "list_path_mappings",
    "parse_radarr_webhook",
    "parse_sonarr_webhook",
    "parse_webhook_payload",
    "resolve_path",
    "resolve_webhook_file",
    "update_instance",
    "update_path_mapping",
]
