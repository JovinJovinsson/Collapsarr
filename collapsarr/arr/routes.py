"""HTTP REST endpoints for arr instance config & path mappings (COL-27).

Thin CRUD layer wrapping :mod:`collapsarr.arr.service`, exposed as a FastAPI
:class:`~fastapi.APIRouter` mounted under ``/api`` by
:func:`collapsarr.main.create_app`. Because everything under ``/api`` is gated
by the API-key middleware (COL-26), every route here inherits key-based auth --
no per-route auth wiring is needed.

Resource shape follows Sonarr/Radarr conventions: an instance is a named
Sonarr/Radarr connection (``/api/instances``), and remote path mappings are
managed per instance as a nested sub-resource
(``/api/instances/{instance_id}/path-mappings``), mirroring how the mappings
belong to a single instance in the data model. Request/response bodies use the
same field names as the ORM models so the surface round-trips cleanly.

Service-layer ``LookupError`` subclasses (:class:`~collapsarr.arr.service.
InstanceNotFoundError`, :class:`~collapsarr.arr.service.PathMappingNotFoundError`)
are translated to ``404`` responses; creation against a missing instance is
likewise a ``404``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_session
from .models import ConnectivityStatus, InstanceType
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

router = APIRouter(prefix="/api", tags=["arr"])


# --- schemas -----------------------------------------------------------------


class InstanceCreate(BaseModel):
    """Request body to create an arr instance."""

    name: str
    type: InstanceType
    base_url: str
    api_key: str


class InstanceUpdate(BaseModel):
    """Request body to update an arr instance; omitted fields are left unchanged.

    ``type`` is not updatable -- switching a configured connection between
    Sonarr and Radarr is a delete-and-recreate, matching the service layer.
    """

    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class InstanceRead(BaseModel):
    """Response shape for an arr instance, including cached connectivity state."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: InstanceType
    base_url: str
    api_key: str
    status: ConnectivityStatus
    status_error: str | None
    status_checked_at: datetime | None
    version: str | None
    created_at: datetime
    updated_at: datetime


class PathMappingCreate(BaseModel):
    """Request body to create a remote path mapping."""

    remote_prefix: str
    local_prefix: str
    order: int = 0


class PathMappingUpdate(BaseModel):
    """Request body to update a path mapping; omitted fields are left unchanged."""

    remote_prefix: str | None = None
    local_prefix: str | None = None
    order: int | None = None


class PathMappingRead(BaseModel):
    """Response shape for a remote path mapping."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    instance_id: int
    remote_prefix: str
    local_prefix: str
    order: int
    created_at: datetime
    updated_at: datetime


# --- instance endpoints ------------------------------------------------------


@router.get("/instances", response_model=list[InstanceRead])
def list_instances_endpoint(session: Session = Depends(get_session)) -> list[object]:
    """List all configured Sonarr/Radarr instances, ordered by id."""
    return list(list_instances(session))


@router.post("/instances", response_model=InstanceRead, status_code=201)
def create_instance_endpoint(
    body: InstanceCreate, session: Session = Depends(get_session)
) -> object:
    """Create an instance; connectivity is validated and cached on save."""
    return create_instance(
        session,
        name=body.name,
        instance_type=body.type,
        base_url=body.base_url,
        api_key=body.api_key,
    )


@router.get("/instances/{instance_id}", response_model=InstanceRead)
def get_instance_endpoint(
    instance_id: int, session: Session = Depends(get_session)
) -> object:
    """Fetch a single instance, or ``404`` if it does not exist."""
    instance = get_instance(session, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"No arr instance with id={instance_id}")
    return instance


@router.put("/instances/{instance_id}", response_model=InstanceRead)
def update_instance_endpoint(
    instance_id: int,
    body: InstanceUpdate,
    session: Session = Depends(get_session),
) -> object:
    """Update an instance's fields and re-validate connectivity."""
    try:
        return update_instance(
            session,
            instance_id,
            name=body.name,
            base_url=body.base_url,
            api_key=body.api_key,
        )
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/instances/{instance_id}", status_code=204)
def delete_instance_endpoint(
    instance_id: int, session: Session = Depends(get_session)
) -> None:
    """Delete an instance (cascades to its path mappings)."""
    try:
        delete_instance(session, instance_id)
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- path-mapping endpoints --------------------------------------------------


def _require_instance(session: Session, instance_id: int) -> None:
    if get_instance(session, instance_id) is None:
        raise HTTPException(status_code=404, detail=f"No arr instance with id={instance_id}")


@router.get(
    "/instances/{instance_id}/path-mappings",
    response_model=list[PathMappingRead],
)
def list_path_mappings_endpoint(
    instance_id: int, session: Session = Depends(get_session)
) -> list[object]:
    """List an instance's path mappings in application order."""
    _require_instance(session, instance_id)
    return list(list_path_mappings(session, instance_id))


@router.post(
    "/instances/{instance_id}/path-mappings",
    response_model=PathMappingRead,
    status_code=201,
)
def create_path_mapping_endpoint(
    instance_id: int,
    body: PathMappingCreate,
    session: Session = Depends(get_session),
) -> object:
    """Create a path mapping under an instance, or ``404`` if it is missing."""
    try:
        return create_path_mapping(
            session,
            instance_id,
            remote_prefix=body.remote_prefix,
            local_prefix=body.local_prefix,
            order=body.order,
        )
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _load_mapping(session: Session, instance_id: int, mapping_id: int) -> object:
    mapping = get_path_mapping(session, mapping_id)
    if mapping is None or mapping.instance_id != instance_id:
        raise HTTPException(
            status_code=404,
            detail=f"No path mapping with id={mapping_id} for instance id={instance_id}",
        )
    return mapping


@router.get(
    "/instances/{instance_id}/path-mappings/{mapping_id}",
    response_model=PathMappingRead,
)
def get_path_mapping_endpoint(
    instance_id: int,
    mapping_id: int,
    session: Session = Depends(get_session),
) -> object:
    """Fetch one path mapping, scoped to its instance."""
    return _load_mapping(session, instance_id, mapping_id)


@router.put(
    "/instances/{instance_id}/path-mappings/{mapping_id}",
    response_model=PathMappingRead,
)
def update_path_mapping_endpoint(
    instance_id: int,
    mapping_id: int,
    body: PathMappingUpdate,
    session: Session = Depends(get_session),
) -> object:
    """Update a path mapping's fields, scoped to its instance."""
    _load_mapping(session, instance_id, mapping_id)
    try:
        return update_path_mapping(
            session,
            mapping_id,
            remote_prefix=body.remote_prefix,
            local_prefix=body.local_prefix,
            order=body.order,
        )
    except PathMappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete(
    "/instances/{instance_id}/path-mappings/{mapping_id}",
    status_code=204,
)
def delete_path_mapping_endpoint(
    instance_id: int,
    mapping_id: int,
    session: Session = Depends(get_session),
) -> None:
    """Delete a path mapping, scoped to its instance."""
    _load_mapping(session, instance_id, mapping_id)
    try:
        delete_path_mapping(session, mapping_id)
    except PathMappingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
