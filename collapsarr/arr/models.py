"""ORM model for a configured Sonarr/Radarr instance connection.

Each :class:`ArrInstance` row is a single Sonarr or Radarr connection: a name,
its type, where to reach it, and the API key to authenticate with. Instances
of either type coexist independently — nothing here assumes at most one
Sonarr or one Radarr.

Connectivity is not verified at the model layer; :mod:`collapsarr.arr.service`
calls :mod:`collapsarr.arr.client` on save and stores the outcome in the
``status``/``status_error``/``version``/``status_checked_at`` columns.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base


class InstanceType(enum.StrEnum):
    """The two supported *arr backends."""

    SONARR = "sonarr"
    RADARR = "radarr"


class ConnectivityStatus(enum.StrEnum):
    """Outcome of the most recent connectivity/version check for an instance."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ArrInstance(Base):
    """A configured Sonarr or Radarr connection.

    ``base_url`` and ``api_key`` are the only fields required to talk to the
    remote instance; the ``status*``/``version`` columns cache the result of
    the last connectivity check performed by the service layer on save.
    """

    __tablename__ = "arr_instances"
    __table_args__ = (UniqueConstraint("name", name="uq_arr_instances_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    type: Mapped[InstanceType] = mapped_column(
        SAEnum(
            InstanceType,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[ConnectivityStatus] = mapped_column(
        SAEnum(
            ConnectivityStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=ConnectivityStatus.UNKNOWN,
    )
    status_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    status_checked_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    version: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return f"ArrInstance(id={self.id!r}, name={self.name!r}, type={self.type!r})"
