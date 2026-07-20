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
from .models import ArrInstance, ConnectivityStatus, InstanceType


class InstanceNotFoundError(LookupError):
    """Raised when an operation targets an instance id that does not exist."""


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
