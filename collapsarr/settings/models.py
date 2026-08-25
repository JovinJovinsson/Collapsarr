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

DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES = 360
"""Default :attr:`GlobalSettings.recently_processed_window_minutes` for a
fresh install / an existing row backfilled by the additive migration
(COL-167). In minutes; ``360`` (6h) matches :data:`collapsarr.config.
Settings.scan_interval_hours`'s own default, which is what this field
replaces as the scheduler's "recently processed" dedup cooldown -- see
:mod:`collapsarr.jobs.scheduler`'s module docstring. ``0`` is a valid,
meaningful value distinct from "unset": it disables the cooldown entirely,
so a file is always eligible for retry regardless of when it was last
processed."""

DEFAULT_AUTO_SET_DEFAULT_AUDIO = False
"""Default :attr:`GlobalSettings.auto_set_default_audio` for a fresh install
/ an existing row backfilled by the additive migration (COL-151). Off by
default -- this is an opt-in behaviour change (automatically flipping which
audio stream carries the Default Audio Track disposition), not something a
fresh install should do without the operator first configuring
``default_audio_language``/``default_audio_channel_tier`` and turning it on
deliberately."""

DEFAULT_DEFAULT_AUDIO_DELAY_MINUTES = 30
"""Default :attr:`GlobalSettings.default_audio_delay_minutes` for a fresh
install / an existing row backfilled by the additive migration (COL-243).
In minutes; modeled on :data:`DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES`'s
column shape (a plain ``NOT NULL`` integer with a matching DB-side
``server_default``). Not yet consumed by any Job-scheduling logic -- this
ticket only adds the knob and its Settings UI field; COL-251 is the
downmix pipeline's delayed Default Audio Track Job enqueue that will read
it."""

DEFAULT_AUTO_QUEUE_PAUSED = False
"""Default :attr:`GlobalSettings.auto_queue_paused` for a fresh install / an
existing row backfilled by the additive migration (COL-174). Off by default,
same rationale as :data:`DEFAULT_AUTO_SET_DEFAULT_AUDIO`: pausing auto-fill
is a deliberate operator action, not something a fresh install should start
doing on its own. See :attr:`GlobalSettings.auto_queue_paused`'s own
docstring for exactly what it does and does not gate."""

DEFAULT_AUTO_PROCESSING_PAUSED = False
"""Default :attr:`GlobalSettings.auto_processing_paused` for a fresh install
/ an existing row backfilled by the additive migration (COL-226). Off by
default, same rationale as :data:`DEFAULT_AUTO_QUEUE_PAUSED`: pausing job
processing is a deliberate operator action, not something a fresh install
should start doing on its own. See :attr:`GlobalSettings.
auto_processing_paused`'s own docstring for exactly what it does and does not
gate -- distinct from :data:`DEFAULT_AUTO_QUEUE_PAUSED`'s "Auto-Queuing
Pause", which only gates the scanner's enqueue/top-up funnel and never
touches an already-``PENDING``/``RUNNING`` Job."""

DEFAULT_AUTO_PROCESSING_PAUSE_RESTORE_VALUE = None
"""Default :attr:`GlobalSettings.auto_processing_pause_restore_value` for a
fresh install / an existing row backfilled by the additive migration
(COL-230). ``None`` (unset) is the only correct default -- unlike
:data:`DEFAULT_AUTO_PROCESSING_PAUSED`, this is not itself an operator-facing
toggle with a meaningful "off" state; it is scratch space the self-update
apply flow (COL-233) writes to for its own one-shot bookkeeping. See
:attr:`GlobalSettings.auto_processing_pause_restore_value`'s own docstring
for the full mechanism."""

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
    limit of 1 (the worker pool's own size, read from this field once at
    startup by :meth:`collapsarr.jobs.queue.JobQueue.from_settings`,
    COL-165), and UI auth disabled.

    ``language_allow_list`` of ``None`` (the default) means "no allow-list --
    evaluate every language present on a file", matching
    :attr:`~collapsarr.downmix.targets.DownmixSettings.language_allow_list`.

    ``api_key`` is auto-generated (via :func:`generate_api_key`) the first time
    the row is created and accepted on every ``/api`` request by
    :class:`collapsarr.auth.enforcement.EnforceAuthMiddleware`.

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
    :class:`collapsarr.auth.enforcement.EnforceAuthMiddleware`), while any
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

    ``default_audio_language``/``default_audio_channel_tier`` (COL-151) are
    the **Preferred Default Audio** setting: a ``(language, channel tier)``
    pair deciding which audio stream on a file *should* carry the
    container's Default Audio Track disposition.
    :class:`~collapsarr.downmix.targets.DownmixTarget` is reused for the
    tier rather than inventing a parallel enum, matching ``enabled_targets``
    above. Both are nullable with no ``server_default``, defaulting to
    ``None`` (unset) for a fresh install and for an existing row backfilled
    by the additive migration -- there is no sensible universal default
    language, so the feature simply has no effect until an operator
    configures both. Validated against :class:`DownmixTarget`'s values by
    :func:`collapsarr.settings.service.update_global_settings`, same
    treatment as ``log_level`` above. Consumed by
    :func:`collapsarr.downmix.default_audio.resolve_default_audio_stream`,
    which decides *which* stream should be default given these two fields
    plus a file's probed audio streams -- actually applying that decision to
    a file is a later ticket's concern (COL-152 automatic, COL-153
    manual/bulk).

    ``auto_set_default_audio`` (COL-151) is the opt-in toggle gating whether
    Collapsarr acts on the preference above automatically; see
    :data:`DEFAULT_AUTO_SET_DEFAULT_AUDIO`. Carries a DB-side
    ``server_default`` (matching ``auth_method``/``auth_required`` above) so
    the additive migration backfills existing installs to ``False`` rather
    than leaving the column ``NULL``.

    ``default_audio_delay_minutes`` (COL-243) is the minimum age (in
    minutes) a remuxed file's new audio streams must have before Collapsarr
    trusts Plex to have already processed them for a Default Audio Track
    write/verify -- see :data:`DEFAULT_DEFAULT_AUDIO_DELAY_MINUTES`. Modeled
    directly on ``recently_processed_window_minutes`` below: a plain
    ``NOT NULL`` integer with a matching DB-side ``server_default`` so the
    additive migration backfills existing installs to the documented
    default rather than leaving the column ``NULL``. Not yet read by any
    Job-scheduling logic -- this ticket only persists the knob and exposes
    it in Settings; COL-251 is the follow-up ticket that consumes it.

    ``recently_processed_window_minutes`` (COL-167) is the scheduler's
    "recently processed" dedup cooldown, in minutes -- see
    :data:`DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES` and
    :mod:`collapsarr.jobs.scheduler`'s module docstring. It used to be
    silently derived from ``scan_interval_hours``; this column decouples the
    two so the cooldown can be tuned independently of scan cadence.
    :class:`~collapsarr.jobs.scheduler.JobScheduler` reads it live from this
    row on every dedup check (unlike ``concurrency_limit``, which the worker
    pool's fixed thread count forces to be read once at startup), so a
    ``PUT /api/settings`` change takes effect on the very next check with no
    restart. ``0`` disables the cooldown entirely (always eligible for
    retry). Carries a DB-side ``server_default`` (matching
    ``auto_set_default_audio`` above) so the additive migration backfills
    existing installs to the documented default rather than leaving the
    column ``NULL``.

    ``auto_queue_paused`` (COL-174, "Auto-Queuing Pause") halts only the
    scanner's Wanted-driven auto-fill -- both the periodic/manual scan's
    initial enqueue and the Auto-Queue Limit's (COL-171) completion/
    cancellation-triggered top-up -- by short-circuiting
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.top_up` (see its
    docstring), the one method every one of those hook points already funnels
    through. It does **not** stop already-``PENDING``/``RUNNING`` Jobs from
    running to completion, and it does **not** gate any manual, explicit
    trigger (:meth:`~collapsarr.jobs.scheduler.JobScheduler.trigger_file`/
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.requeue_file`/
    :meth:`~collapsarr.jobs.scheduler.JobScheduler.requeue_all_failed`) --
    those never call :meth:`top_up` in the first place, so they are
    unaffected by construction, not by an extra check. Read live from this
    row on every :meth:`top_up` call, same treatment as
    ``recently_processed_window_minutes`` above, so a ``PUT /api/settings``
    change takes effect on the very next auto-fill attempt with no restart
    or scheduler reconstruction. Carries a DB-side ``server_default`` so the
    additive migration backfills existing installs to ``False`` (auto-fill
    stays on unless an operator explicitly pauses it) rather than leaving the
    column ``NULL``.

    ``auto_processing_paused`` (COL-226, "Auto-Processing Pause") halts the
    Job Queue's sole pending -> running claim chokepoint
    (:meth:`~collapsarr.jobs.queue.JobQueue._claim_next`) -- while set, a
    free worker never claims a new ``PENDING`` Job, so nothing new starts
    running. It is deliberately distinct from ``auto_queue_paused`` above:
    that gate only stops the scanner's enqueue/top-up funnel from *adding*
    new ``PENDING`` Jobs, and never touches a Job already sitting
    ``PENDING``; this gate instead stops already-``PENDING`` Jobs (however
    they got there -- scan, manual trigger, requeue) from ever being
    claimed. A Job a worker has *already* claimed (``RUNNING``) is
    unaffected either way -- it keeps running to completion, since this gate
    only ever guards the claim step, never an in-flight job. Read live by
    :meth:`~collapsarr.jobs.queue.JobQueue._claim_next` via an injected
    ``pause_check`` callable (:meth:`~collapsarr.jobs.queue.JobQueue.
    from_settings` wires a real one reading this column), so a ``PUT
    /api/settings`` change takes effect on the next claim attempt with no
    restart -- same "read fresh" treatment as
    ``recently_processed_window_minutes`` above. Carries a DB-side
    ``server_default`` so the additive migration backfills existing installs
    to ``False`` (processing stays on unless an operator explicitly pauses
    it) rather than leaving the column ``NULL``.

    ``ffmpeg_path`` (COL-218) is an optional override for the FFmpeg
    executable Collapsarr invokes -- e.g. the absolute path to a
    runtime-free native build (Epic COL-214) rather than one resolved off
    ``PATH``. Nullable with no ``server_default``, same treatment as
    ``default_audio_language``/``log_level`` above: ``None`` (unset) is a
    *meaningful* value, not just "not migrated yet" -- it means "keep
    resolving the bare ``\"ffmpeg\"`` command off ``PATH``", which is exactly
    today's behaviour, so an existing install's row is backfilled to
    ``NULL`` and sees no change. Read live from this row -- no restart
    required -- by :meth:`collapsarr.jobs.queue.JobQueue._resolve_pipeline_kwargs`
    (threaded into every enqueued downmix/default-audio job's ``ffmpeg_path``
    kwarg when set) and by :func:`collapsarr.health.ffmpeg.
    make_ffmpeg_check_run`'s ``run`` callable (probes this path instead of
    the bare default when set).

    ``auto_processing_pause_restore_value`` (COL-230, consumed by COL-233) is
    a nullable *scratch* boolean backing the self-update apply flow's
    one-shot "pause processing, apply the update, then restore whatever the
    pause state was before" mechanism -- distinct from every other field on
    this row, which are all operator-facing settings; this one is internal
    bookkeeping the self-update flow itself reads and writes. ``None`` is the
    steady-state value (no self-update is currently in flight, or the flow
    hasn't yet decided it needs to touch ``auto_processing_paused``); COL-233
    is expected to snapshot the current ``auto_processing_paused`` value here
    immediately before force-pausing it for the duration of an apply, then
    restore ``auto_processing_paused`` from it and reset this field back to
    ``None`` once the update completes (success, failure, or rollback) -- so
    an apply that force-paused processing never leaves it paused
    indefinitely. Nullable with no ``server_default``, same treatment as
    ``ffmpeg_path`` above -- an existing install's row is backfilled to
    ``NULL`` (no in-flight self-update) and sees no change.
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

    default_audio_language: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None
    )
    default_audio_channel_tier: Mapped[str | None] = mapped_column(
        String(10), nullable=True, default=None
    )
    auto_set_default_audio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=DEFAULT_AUTO_SET_DEFAULT_AUDIO,
        server_default=text("0"),
    )

    default_audio_delay_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_DEFAULT_AUDIO_DELAY_MINUTES,
        server_default=text(str(DEFAULT_DEFAULT_AUDIO_DELAY_MINUTES)),
    )

    recently_processed_window_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES,
        server_default=text(str(DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES)),
    )

    auto_queue_paused: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=DEFAULT_AUTO_QUEUE_PAUSED,
        server_default=text("0"),
    )

    auto_processing_paused: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=DEFAULT_AUTO_PROCESSING_PAUSED,
        server_default=text("0"),
    )

    ffmpeg_path: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)

    auto_processing_pause_restore_value: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=DEFAULT_AUTO_PROCESSING_PAUSE_RESTORE_VALUE
    )

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return (
            f"GlobalSettings(id={self.id!r}, enabled_targets={self.enabled_targets!r}, "
            f"concurrency_limit={self.concurrency_limit!r}, "
            f"ui_auth_enabled={self.ui_auth_enabled!r})"
        )
