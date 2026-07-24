"""ORM model for the singleton global application settings row (COL-24).

Per the product design (``docs/plans/2026-07-20-collapsarr-v1-design.md``),
Settings covers: enabled downmix targets, language allow-list, codec/bitrate
overrides, concurrency limit, and the UI auth toggle. (Notification config --
webhook/Discord -- is a separate, dedicated model owned by the "Connect &
Notifications" epic's own ticket, COL-35; it is deliberately not part of this
row.)

:class:`~collapsarr.downmix.targets.DownmixTarget` is reused rather than
inventing a parallel enum -- :mod:`collapsarr.downmix.targets` already notes
that its plain-dataclass ``DownmixSettings`` is "settings-*shaped*" and meant
to be adapted into a real, persisted model later; this is that adaptation.
``enabled_targets``/``language_allow_list`` are stored as comma-joined
strings (same convention :mod:`collapsarr.jobs.history` uses for its
``target``/``language`` columns) rather than a JSON column, since nothing
else in this codebase uses one yet.

``GlobalSettings`` is a **singleton** table: the ``id`` column is
constrained to always equal :data:`SETTINGS_ID`, so at most one row can ever
exist (a second row would need a second, distinct primary key value, which
the check constraint forbids). :mod:`collapsarr.settings.service` is the
only intended way to read or create that row.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from collapsarr.database import Base
from collapsarr.downmix.targets import DownmixTarget

SETTINGS_ID = 1
"""The fixed primary key of the single :class:`GlobalSettings` row."""

API_KEY_BYTES = 16
"""Entropy of a generated API key. 16 bytes renders as a 32-char hex string,
matching Sonarr/Radarr's own API-key format."""

SESSION_SECRET_BYTES = 32
"""Entropy of a generated session secret (256 bits, a 64-char hex string)."""

AUTH_METHOD_FORMS = "forms"
AUTH_METHOD_BASIC = "basic"
"""How the UI credential is presented: a forms login or HTTP Basic auth."""

AUTH_REQUIRED_ENABLED = "enabled"
AUTH_REQUIRED_LOCAL_BYPASS = "local_bypass"
"""Whether auth is always required, or bypassed for local-network callers."""


def generate_api_key() -> str:
    """Return a fresh, cryptographically-random API key.

    A 32-character lowercase hex string (``secrets.token_hex(16)``), matching
    the *arr-family convention so the key is a drop-in for existing tooling.
    Used as the column default so a key is minted automatically the first time
    the singleton settings row is created (see
    :func:`collapsarr.settings.service.get_global_settings`).
    """
    return secrets.token_hex(API_KEY_BYTES)


def generate_session_secret() -> str:
    """Return a fresh, cryptographically-random session-signing secret.

    A 64-character lowercase hex string (``secrets.token_hex(32)``). Minted
    once when the singleton settings row is first created -- mirroring
    :func:`generate_api_key` -- and stable thereafter, so signed session
    cookies survive restarts (see
    :func:`collapsarr.settings.service.get_global_settings`, which also
    backfills it for rows that predate the column).
    """
    return secrets.token_hex(SESSION_SECRET_BYTES)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GlobalSettings(Base):
    """The single row of global, persisted application settings.

    Defaults match the PRD exactly: Stereo enabled by default with 2.1/5.1
    opt-in (``enabled_targets`` defaults to just ``"stereo"``), AAC for the
    Stereo target, AC3 @ 448kbps for the surround targets, a concurrency
    limit of 1 (matching :attr:`collapsarr.config.Settings.job_max_concurrency`'s
    own default), and UI auth disabled.

    ``language_allow_list`` of ``None`` (the default) means "no allow-list --
    evaluate every language present on a file", matching
    :attr:`~collapsarr.downmix.targets.DownmixSettings.language_allow_list`.

    ``api_key`` is auto-generated (via :func:`generate_api_key`) the first time
    the row is created and accepted on every ``/api`` request by
    :func:`collapsarr.auth.enforcement.enforce_auth_middleware`.

    The auth-credential columns (COL-49) hold the single UI operator credential
    -- Radarr-style, no multi-user. ``auth_username``/``auth_password_hash`` are
    ``None`` until a credential is set; the hash is a PBKDF2 encoding (see
    :mod:`collapsarr.settings.passwords`), never plaintext. ``auth_method``
    (``forms``|``basic``) and ``auth_required`` (``enabled``|``local_bypass``)
    carry DB-side ``server_default``\\ s so the schema-ensure step (COL-48) can add
    them ``NOT NULL`` to existing installs. ``session_secret`` is minted once on
    row creation, mirroring ``api_key``; it is nullable at the DB level so the
    schema-ensure can add it to an existing row, which
    :func:`collapsarr.settings.service.get_global_settings` then backfills.

    ``auth_required`` defaults to ``local_bypass`` (COL-51), not ``enabled``: a
    fresh install stays frictionless for a caller connecting from a
    loopback/private-network address (see
    :func:`collapsarr.auth.enforcement.enforce_auth_middleware`), while any
    routable address must still authenticate -- closing the "wide open on
    0.0.0.0" gap without any pre-launch configuration. An install sitting
    behind a reverse proxy should switch this to ``enabled``, since
    classification only ever looks at the direct TCP peer (see that module's
    docstring).
    """

    __tablename__ = "global_settings"
    __table_args__ = (
        CheckConstraint(f"id = {SETTINGS_ID}", name="ck_global_settings_singleton"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=SETTINGS_ID)
    api_key: Mapped[str] = mapped_column(String(64), nullable=False, default=generate_api_key)
    enabled_targets: Mapped[str] = mapped_column(
        String(50), nullable=False, default=DownmixTarget.STEREO.value
    )
    language_allow_list: Mapped[str | None] = mapped_column(
        String(500), nullable=True, default=None
    )
    stereo_codec: Mapped[str] = mapped_column(String(50), nullable=False, default="aac")
    stereo_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    surround_codec: Mapped[str] = mapped_column(String(50), nullable=False, default="ac3")
    surround_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True, default=448)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ui_auth_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    auth_username: Mapped[str | None] = mapped_column(String(150), nullable=True, default=None)
    auth_password_hash: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=None
    )
    auth_method: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=AUTH_METHOD_FORMS,
        server_default=text(f"'{AUTH_METHOD_FORMS}'"),
    )
    auth_required: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AUTH_REQUIRED_LOCAL_BYPASS,
        server_default=text(f"'{AUTH_REQUIRED_LOCAL_BYPASS}'"),
    )
    session_secret: Mapped[str | None] = mapped_column(
        String(128), nullable=True, default=generate_session_secret
    )

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"GlobalSettings(id={self.id!r}, enabled_targets={self.enabled_targets!r}, "
            f"concurrency_limit={self.concurrency_limit!r}, "
            f"ui_auth_enabled={self.ui_auth_enabled!r})"
        )
