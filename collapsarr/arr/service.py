"""Service-layer CRUD for :class:`~collapsarr.arr.models.ArrInstance`.

HTTP exposure is a separate (API) epic's concern — this module is the whole
surface: plain functions taking a SQLAlchemy :class:`~sqlalchemy.orm.Session`
and returning/mutating ORM objects. Creating or updating an instance always
re-validates connectivity via :func:`collapsarr.arr.client.check_connectivity`
and persists the outcome, per COL-11's acceptance criteria.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .client import check_connectivity
from .models import ArrInstance, ConnectivityStatus, InstanceType, RemotePathMapping


class InstanceNotFoundError(LookupError):
    """Raised when an operation targets an instance id that does not exist."""


class PathMappingNotFoundError(LookupError):
    """Raised when an operation targets a path-mapping id that does not exist."""


def _apply_connectivity_check(
    instance: ArrInstance, *, transport: httpx.BaseTransport | None
) -> None:
    """Run the connectivity/version check and stamp its outcome onto ``instance``."""
    result = check_connectivity(instance.base_url, instance.api_key, transport=transport)
    instance.status = ConnectivityStatus.OK if result.ok else ConnectivityStatus.ERROR
    instance.status_error = result.error
    instance.version = result.version
    instance.status_checked_at = datetime.now(UTC)


def create_instance(
    session: Session,
    *,
    name: str,
    instance_type: InstanceType,
    base_url: str,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
) -> ArrInstance:
    """Create a new instance config and validate connectivity before returning it.

    The row is persisted regardless of whether the connectivity check
    succeeds — ``status``/``status_error`` record the outcome so failed
    instances remain visible (and editable) rather than being silently
    dropped.
    """
    instance = ArrInstance(
        name=name,
        type=instance_type,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
    )
    _apply_connectivity_check(instance, transport=transport)
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance


def list_instances(session: Session) -> list[ArrInstance]:
    """Return all configured instances, both types, ordered by id."""
    return list(session.scalars(select(ArrInstance).order_by(ArrInstance.id)))


def get_instance(session: Session, instance_id: int) -> ArrInstance | None:
    """Return the instance with ``instance_id``, or ``None`` if it doesn't exist."""
    return session.get(ArrInstance, instance_id)


def update_instance(
    session: Session,
    instance_id: int,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ArrInstance:
    """Update the given fields on an instance and re-validate connectivity.

    Only fields passed explicitly (non-``None``) are changed. Connectivity is
    re-checked on every save, matching :func:`create_instance`, so a fixed API
    key or corrected URL is reflected in ``status`` immediately.

    Raises:
        InstanceNotFoundError: if ``instance_id`` does not exist.
    """
    instance = get_instance(session, instance_id)
    if instance is None:
        raise InstanceNotFoundError(f"No arr instance with id={instance_id}")

    if name is not None:
        instance.name = name
    if base_url is not None:
        instance.base_url = base_url.rstrip("/")
    if api_key is not None:
        instance.api_key = api_key

    _apply_connectivity_check(instance, transport=transport)
    session.commit()
    session.refresh(instance)
    return instance


def delete_instance(session: Session, instance_id: int) -> None:
    """Delete an instance config.

    Raises:
        InstanceNotFoundError: if ``instance_id`` does not exist.
    """
    instance = get_instance(session, instance_id)
    if instance is None:
        raise InstanceNotFoundError(f"No arr instance with id={instance_id}")
    session.delete(instance)
    session.commit()


def list_path_mappings(session: Session, instance_id: int) -> list[RemotePathMapping]:
    """Return an instance's path mappings in their configured application order.

    Used by the webhook handler (COL-14) to resolve a webhook-reported remote
    path to a local one via :func:`collapsarr.arr.models.resolve_path`. Does
    not raise for an unknown ``instance_id`` -- it simply returns an empty
    list, matching :func:`resolve_path`'s "no mappings" pass-through.
    """
    return list(
        session.scalars(
            select(RemotePathMapping)
            .where(RemotePathMapping.instance_id == instance_id)
            .order_by(RemotePathMapping.order)
        )
    )


def get_path_mapping(session: Session, mapping_id: int) -> RemotePathMapping | None:
    """Return the path mapping with ``mapping_id``, or ``None`` if it doesn't exist."""
    return session.get(RemotePathMapping, mapping_id)


def create_path_mapping(
    session: Session,
    instance_id: int,
    *,
    remote_prefix: str,
    local_prefix: str,
    order: int = 0,
) -> RemotePathMapping:
    """Create a path mapping for an instance and return it.

    Raises:
        InstanceNotFoundError: if ``instance_id`` does not exist. Mappings are
            meaningless without their instance, so creation is rejected rather
            than leaving a dangling row.
    """
    if get_instance(session, instance_id) is None:
        raise InstanceNotFoundError(f"No arr instance with id={instance_id}")

    mapping = RemotePathMapping(
        instance_id=instance_id,
        remote_prefix=remote_prefix,
        local_prefix=local_prefix,
        order=order,
    )
    session.add(mapping)
    session.commit()
    session.refresh(mapping)
    return mapping


def update_path_mapping(
    session: Session,
    mapping_id: int,
    *,
    remote_prefix: str | None = None,
    local_prefix: str | None = None,
    order: int | None = None,
) -> RemotePathMapping:
    """Update the given fields on a path mapping and return it.

    Only fields passed explicitly (non-``None``) are changed, matching
    :func:`update_instance`.

    Raises:
        PathMappingNotFoundError: if ``mapping_id`` does not exist.
    """
    mapping = get_path_mapping(session, mapping_id)
    if mapping is None:
        raise PathMappingNotFoundError(f"No path mapping with id={mapping_id}")

    if remote_prefix is not None:
        mapping.remote_prefix = remote_prefix
    if local_prefix is not None:
        mapping.local_prefix = local_prefix
    if order is not None:
        mapping.order = order

    session.commit()
    session.refresh(mapping)
    return mapping


def delete_path_mapping(session: Session, mapping_id: int) -> None:
    """Delete a path mapping.

    Raises:
        PathMappingNotFoundError: if ``mapping_id`` does not exist.
    """
    mapping = get_path_mapping(session, mapping_id)
    if mapping is None:
        raise PathMappingNotFoundError(f"No path mapping with id={mapping_id}")
    session.delete(mapping)
    session.commit()
