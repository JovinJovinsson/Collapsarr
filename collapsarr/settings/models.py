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

from sqlalchemy import Boolean, CheckConstraint, Float, Integer, String, text
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

DEFAULT_BACKUP_INTERVAL_DAYS = 7
"""Default days between scheduled backups (COL-66). Consumed by the
scheduler landing in COL-67 -- this ticket only persists the knob."""

DEFAULT_BACKUP_RETENTION_DAYS = 28
"""Default days a backup is kept before pruning (COL-66). Consumed by the
retention pruning landing in COL-68 -- this ticket only persists the knob."""

DEFAULT_DISK_SPACE_WARNING_PERCENT = 5.0
"""Default free-space percentage below which the disk-space health check
(COL-79) reports a warning (``WARN-DISK-001``). Consumed live -- on every
check tick, not just at process start -- by
:func:`collapsarr.health.disk_space.make_disk_space_check_run`."""

DEFAULT_DISK_SPACE_ERROR_PERCENT = 2.0
"""Default free-space percentage below which the disk-space health check
(COL-79) escalates to an error (``ERR-DISK-001``). Deliberately lower than
:data:`DEFAULT_DISK_SPACE_WARNING_PERCENT` so the error tier is strictly
worse than the warning tier by default, but the two fields are validated and
stored independently -- see
:func:`collapsarr.settings.service.update_global_settings`."""

UPDATE_CHANNEL_STABLE = "stable"
UPDATE_CHANNEL_BETA = "beta"
"""Which GitHub Release stream the Update Check (COL-86, ``CONTEXT.md``'s
"Release Channel") compares the running instance against. ``stable`` is the
latest non-prerelease Release; ``beta`` is the latest prerelease Release
(comparison logic for the beta channel is a later ticket -- COL-86 only
persists the knob and always fetches the stable channel's latest release)."""

DEFAULT_TRACKED = True
"""Default :attr:`GlobalSettings.default_tracked` for a fresh install / an
existing row backfilled by the additive migration (COL-98). A Library node
whose ancestry carries no explicit **Tracked** override falls back to this
instance-wide default; ``True`` means Collapsarr acts automatically on newly
discovered media unless the user opts a subtree out (see ``CONTEXT.md``'s
"Tracked")."""

DEFAULT_UPDATE_CHANNEL = UPDATE_CHANNEL_STABLE
"""Default :attr:`GlobalSettings.update_channel` for a fresh install / an
existing row backfilled by the additive migration. This is the ORM/DB-level
default (the column's ``default=``/``server_default=``); a fresh row's
*actual* value is decided by :func:`collapsarr.settings.service.
get_global_settings` at creation time, which overrides it with ``"beta"``
when the running build is itself a beta build (COL-88) -- see
:data:`BETA_LOCAL_SEGMENT_PREFIX`."""

LOG_LEVEL_DEBUG = "DEBUG"
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_WARNING = "WARNING"
LOG_LEVEL_ERROR = "ERROR"
LOG_LEVELS = (LOG_LEVEL_DEBUG, LOG_LEVEL_INFO, LOG_LEVEL_WARNING, LOG_LEVEL_ERROR)
"""The four levels settable from Settings -> General's log-level dropdown
(COL-130), matching Python's own level names (excluding ``CRITICAL``/
``NOTSET``, which the dropdown doesn't expose). Validated against by
:func:`collapsarr.settings.service.update_global_settings`, same treatment as
:data:`UPDATE_CHANNEL_STABLE`/:data:`UPDATE_CHANNEL_BETA` above."""

BETA_LOCAL_SEGMENT_PREFIX = "+beta"
"""The bare PEP 440 local-version marker a beta build carries in its running
``collapsarr.__version__`` (COL-96) -- e.g. ``"0.2.1.0007+beta"``, stamped by
``.github/workflows/beta.yml``'s ``build-wheel`` job. It is purely a *marker*
now: the build's ordering identity lives in the release segment
(``<base>.<build>``) ahead of it, so nothing follows ``+beta`` (COL-88's old
scheme appended a ``.<short-sha>`` here -- COL-96 dropped it). Used by
:func:`collapsarr.settings.service.get_global_settings` (a substring check) to
auto-default a fresh install's ``update_channel`` to ``"beta"`` when the
running build is itself a beta build. This is the single source of truth for
the literal: :mod:`collapsarr.update_check.comparison` imports it directly for
its own beta-channel version comparison rather than redefining it --
``update_check`` already imports from :mod:`collapsarr.settings.models`/
:mod:`collapsarr.settings.service` elsewhere (e.g. :mod:`collapsarr.
update_check.scheduler`), and nothing in :mod:`collapsarr.settings` imports
:mod:`collapsarr.update_check`, so there is no circular import risk."""


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

    ``backup_interval_days``/``backup_retention_days`` (COL-66) are the two
    knobs later slices consume: COL-67's scheduler reads the interval to decide
    when to take the next scheduled backup, and COL-68's pruning reads the
    retention to decide how long a backup is kept before deletion. Both carry
    DB-side ``server_default``\\ s (matching ``auth_method``/``auth_required``
    above) so the additive migration backfills existing installs with the
    documented defaults (7 / 28 days) rather than leaving them ``NULL``.

    ``update_channel`` (COL-86, ``CONTEXT.md``'s "Release Channel") is
    ``stable``|``beta``, selecting which GitHub Release stream the Update
    Check (:mod:`collapsarr.update_check`) compares the running instance
    against. Carries the same ``server_default`` treatment as
    ``auth_method``/``auth_required`` above so an existing install's row is
    backfilled to ``stable`` in the same additive migration. Validated to the
    two-value enum by :func:`collapsarr.settings.service.
    update_global_settings` (COL-88) and read/write through the Settings page
    (``GET``/``PUT /api/settings``) same as every other setting.

    ``disk_space_warning_percent``/``disk_space_error_percent`` (COL-79) are
    the two free-space-percentage thresholds
    :func:`collapsarr.health.disk_space.make_disk_space_check_run` reads --
    live, from this row, on every scheduler tick, so editing them via Settings
    takes effect on the next tick without a restart. Same
    additive-with-``server_default`` treatment as the backup columns above.
    The two are stored and validated independently (see
    :func:`collapsarr.settings.service.update_global_settings`); there is no
    DB-level constraint forcing the error threshold below the warning
    threshold, matching how ``backup_interval_days``/``backup_retention_days``
    also carry no cross-field constraint.

    ``log_level`` (COL-130) is ``DEBUG``|``INFO``|``WARNING``|``ERROR``, or
    ``None`` -- unlike every other field on this row, ``None`` is a
    *meaningful* value, not just "not migrated yet": it means "no override,
    fall back to the ``COLLAPSARR_LOG_LEVEL`` environment setting" (default
    ``INFO``), resolved once at boot by
    :func:`collapsarr.logging_setup.configure_logging`. Nullable at the DB
    level with no ``server_default`` -- an existing install's row is
    backfilled to ``NULL`` (not a concrete level) by the additive migration,
    which is exactly the "still deferring to the env setting" behaviour it
    already had. Validated against :data:`LOG_LEVELS` by
    :func:`collapsarr.settings.service.update_global_settings`, same
    treatment as ``update_channel`` above. A write here (through ``PUT
    /api/settings``) is applied live to the ``collapsarr`` logger's effective
    level and rotating file handler's ``backupCount`` by
    :func:`collapsarr.logging_setup.apply_log_level` -- called from
    :mod:`collapsarr.settings.routes`, mirroring how
    :func:`rotate_session_secret`'s caller pushes the fresh secret into the
    running process's cached copy -- and a persisted level survives a
    restart, re-applied once the database is available during
    :func:`collapsarr.main.create_app`'s lifespan (after the env-sourced boot
    floor from ``configure_logging`` above has already run).
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

    backup_interval_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_BACKUP_INTERVAL_DAYS,
        server_default=text(str(DEFAULT_BACKUP_INTERVAL_DAYS)),
    )
    backup_retention_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_BACKUP_RETENTION_DAYS,
        server_default=text(str(DEFAULT_BACKUP_RETENTION_DAYS)),
    )

    disk_space_warning_percent: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=DEFAULT_DISK_SPACE_WARNING_PERCENT,
        server_default=text(str(DEFAULT_DISK_SPACE_WARNING_PERCENT)),
    )
    disk_space_error_percent: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=DEFAULT_DISK_SPACE_ERROR_PERCENT,
        server_default=text(str(DEFAULT_DISK_SPACE_ERROR_PERCENT)),
    )

    update_channel: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=DEFAULT_UPDATE_CHANNEL,
        server_default=text(f"'{DEFAULT_UPDATE_CHANNEL}'"),
    )

    default_tracked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=DEFAULT_TRACKED,
        server_default=text("1"),
    )

    log_level: Mapped[str | None] = mapped_column(String(10), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"GlobalSettings(id={self.id!r}, enabled_targets={self.enabled_targets!r}, "
            f"concurrency_limit={self.concurrency_limit!r}, "
            f"ui_auth_enabled={self.ui_auth_enabled!r})"
        )
