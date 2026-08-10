"""Service-layer read/write interface for global settings (COL-24).

Plain functions taking a SQLAlchemy :class:`~sqlalchemy.orm.Session`,
matching the pattern already used by :mod:`collapsarr.arr.service` and
:mod:`collapsarr.jobs.history`. HTTP exposure (a future Settings page) is a
separate epic's concern -- this module is the whole surface.

Named ``get_global_settings``/``update_global_settings`` rather than
``get_settings``/``update_settings`` to avoid colliding (in intent, not just
in import path) with :func:`collapsarr.config.get_settings`, which returns
the process's *environment*-sourced :class:`~collapsarr.config.Settings` --
a distinct concept from the DB-persisted :class:`~collapsarr.settings.
models.GlobalSettings` row this module manages.

:func:`get_global_settings` is get-or-create: it returns the singleton row,
creating it with documented defaults on first call if it doesn't exist yet
(the "single Settings row exists with documented defaults on first run"
acceptance criterion). :func:`update_global_settings` changes only the
fields passed explicitly; the three nullable fields (``language_allow_list``,
``stereo_bitrate_kbps``, ``surround_bitrate_kbps``) use the :data:`_UNSET`
sentinel rather than a bare ``None`` default so that "not provided" can be
told apart from "explicitly clear this override".

:func:`as_downmix_settings` adapts a persisted row into a
:class:`~collapsarr.downmix.targets.DownmixSettings`, the plain-dataclass
shape :mod:`collapsarr.downmix.targets` already documents as the eventual
target of "a real settings model" -- this is that adaptation, ready for the
downmix pipeline (COL-25 and beyond) to consume.

``update_channel`` (COL-88) gets two extra pieces of behaviour beyond the
plain "only change what's passed" rule every other field follows:
:func:`get_global_settings` picks a version-aware default the first time the
row is ever created (``"beta"`` for a beta build, otherwise ``"stable"``),
and :func:`update_global_settings` validates any passed value against the
``stable``/``beta`` enum, raising :class:`ValueError` otherwise -- the
Settings-page write path (:mod:`collapsarr.settings.routes`) also restricts
the field to a ``Literal`` at the API boundary, but this is the single
service-layer guard every other caller (env-seeding, scripts, tests) goes
through too.

``log_level`` (COL-130) follows ``language_allow_list``'s nullable-clearable
convention (the :data:`_UNSET` sentinel), not ``update_channel``'s: an
explicit ``None`` clears a persisted override back to "defer to
``COLLAPSARR_LOG_LEVEL``", while omitting the argument leaves whatever is
already stored untouched. A non-``None`` value is validated against
:data:`~collapsarr.settings.models.LOG_LEVELS`, raising :class:`ValueError`
otherwise -- same guard shape as ``update_channel``. This function only
*persists* the value; applying it live to the running ``collapsarr`` logger
is the caller's job (:func:`collapsarr.settings.routes.update_settings_endpoint`
calls :func:`collapsarr.logging_setup.apply_log_level`), mirroring how
:func:`rotate_session_secret`'s caller pushes the fresh secret into the
running process's own cached copy.

``default_audio_language``/``default_audio_channel_tier`` (COL-151) both
follow ``log_level``'s nullable-clearable convention (the :data:`_UNSET`
sentinel): an explicit ``None`` clears a configured Preferred Default Audio
preference, while omitting the argument leaves whatever is already stored
untouched. ``default_audio_channel_tier`` takes a
:class:`~collapsarr.downmix.targets.DownmixTarget` (not a bare string,
matching ``enabled_targets``' own typing) and is stored as its ``.value``.
``auto_set_default_audio`` follows the plain "only change what's passed"
rule every other boolean field here follows (``ui_auth_enabled``,
``default_tracked``) -- there is no clear-to-default sentinel since it
always holds a concrete ``True``/``False``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from collapsarr import __version__
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget

from .models import (
    BETA_LOCAL_SEGMENT_PREFIX,
    LOG_LEVELS,
    SETTINGS_ID,
    UPDATE_CHANNEL_BETA,
    UPDATE_CHANNEL_STABLE,
    GlobalSettings,
    generate_session_secret,
)
from .passwords import hash_password, verify_password


class _Unset:
    """Sentinel type distinguishing an omitted keyword argument from ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNSET"


_UNSET = _Unset()


def _encode_targets(targets: frozenset[DownmixTarget]) -> str:
    """Comma-join enabled targets' values, sorted for a deterministic string."""
    return ",".join(sorted(target.value for target in targets))


def _decode_targets(value: str) -> frozenset[DownmixTarget]:
    """Inverse of :func:`_encode_targets`; an empty string decodes to no targets."""
    if not value:
        return frozenset()
    return frozenset(DownmixTarget(item) for item in value.split(","))


def _encode_languages(languages: frozenset[str] | None) -> str | None:
    """Comma-join a language allow-list, sorted; ``None`` stays ``None`` (no allow-list)."""
    if languages is None:
        return None
    return ",".join(sorted(languages))


def _decode_languages(value: str | None) -> frozenset[str] | None:
    """Inverse of :func:`_encode_languages`."""
    if value is None:
        return None
    return frozenset(value.split(","))


def _default_update_channel(version: str) -> str:
    """Return the ``update_channel`` a fresh install's row should default to (COL-88).

    ``"beta"`` when the running ``version`` carries a
    :data:`~collapsarr.settings.models.BETA_LOCAL_SEGMENT_PREFIX` local
    segment -- i.e. the running build is itself a beta build, so it makes
    sense to keep tracking that channel by default. Otherwise ``"stable"``,
    the documented default for every ordinary install.
    """
    if BETA_LOCAL_SEGMENT_PREFIX in version:
        return UPDATE_CHANNEL_BETA
    return UPDATE_CHANNEL_STABLE


def get_global_settings(session: Session) -> GlobalSettings:
    """Return the singleton settings row, creating it with defaults if absent.

    Safe to call repeatedly and from multiple call sites (job queue,
    downmix pipeline, a future web UI) -- once created, the same row is
    always returned; it is never recreated or duplicated (enforced at the
    schema level by :class:`~collapsarr.settings.models.GlobalSettings`'s
    singleton check constraint).

    ``update_channel`` is seeded with :func:`_default_update_channel` rather
    than the column's plain static default -- a fresh install running a beta
    build (``collapsarr.__version__`` carrying a ``+beta`` local segment)
    starts already tracking the beta channel (COL-88).
    """
    settings = session.get(GlobalSettings, SETTINGS_ID)
    if settings is None:
        settings = GlobalSettings(
            id=SETTINGS_ID, update_channel=_default_update_channel(__version__)
        )
        session.add(settings)
        session.commit()
        session.refresh(settings)
    elif settings.session_secret is None:
        # An install that predates the ``session_secret`` column (added to the
        # existing row NULL by the schema-ensure step) gets its secret minted
        # once here, then it is stable for every subsequent read -- matching the
        # "generated once on row creation" guarantee for fresh installs.
        settings.session_secret = generate_session_secret()
        session.commit()
        session.refresh(settings)
    return settings


def update_global_settings(
    session: Session,
    *,
    enabled_targets: frozenset[DownmixTarget] | None = None,
    language_allow_list: frozenset[str] | None | _Unset = _UNSET,
    stereo_codec: str | None = None,
    stereo_bitrate_kbps: int | None | _Unset = _UNSET,
    surround_codec: str | None = None,
    surround_bitrate_kbps: int | None | _Unset = _UNSET,
    concurrency_limit: int | None = None,
    ui_auth_enabled: bool | None = None,
    auth_username: str | None | _Unset = _UNSET,
    password: str | None | _Unset = _UNSET,
    auth_method: str | None = None,
    auth_required: str | None = None,
    backup_interval_days: int | None = None,
    backup_retention_days: int | None = None,
    disk_space_warning_percent: float | None = None,
    disk_space_error_percent: float | None = None,
    update_channel: str | None = None,
    default_tracked: bool | None = None,
    log_level: str | None | _Unset = _UNSET,
    default_audio_language: str | None | _Unset = _UNSET,
    default_audio_channel_tier: DownmixTarget | None | _Unset = _UNSET,
    auto_set_default_audio: bool | None = None,
) -> GlobalSettings:
    """Update the given fields on the settings row and return it.

    Only fields passed explicitly are changed, matching the convention
    :func:`collapsarr.arr.service.update_instance` already uses. For the
    fields whose valid domain includes ``None`` as a meaningful value
    (``language_allow_list``, ``stereo_bitrate_kbps``,
    ``surround_bitrate_kbps``, ``auth_username``, and ``password``), passing
    ``None`` explicitly *clears* the stored value (e.g. removes a bitrate
    override, or unsets the credential) -- omitting the argument (the default)
    leaves it untouched. Creates the row with defaults first if it doesn't
    exist yet, same as :func:`get_global_settings`.

    ``password`` takes the **plaintext** credential and is stored as a PBKDF2
    hash (:func:`collapsarr.settings.passwords.hash_password`); the plaintext
    itself is never persisted. Verify a candidate later with
    :func:`verify_auth_password`.

    ``backup_interval_days``/``backup_retention_days`` (COL-66) follow the
    same "only change what's passed" rule as every other non-nullable field
    here -- there is no clear-to-default sentinel since both always hold a
    positive integer (the DB-side ``server_default`` only matters for
    pre-existing rows predating the columns, not for updates).

    ``disk_space_warning_percent``/``disk_space_error_percent`` (COL-79)
    follow the same rule as the backup pair above. The health check
    (:mod:`collapsarr.health.disk_space`) reads them straight off this row on
    every tick, so a change here is live on the *next* tick with no restart.

    ``update_channel`` (COL-88) follows the same "only change what's passed"
    rule, but is additionally validated against the ``stable``/``beta``
    enum -- an unrecognised value raises :class:`ValueError` rather than
    being written, so no caller (the Settings-page write path or otherwise)
    can persist an invalid channel. The Update Check scheduler reads this
    live from the row on every tick, so a change here takes effect on the
    next tick with no restart.

    ``default_tracked`` (COL-98) follows the same "only change what's passed"
    rule as every other boolean field here. It is the instance-wide fallback
    a Library node's Tracked value resolves to when nothing in its ancestry
    carries an explicit override (see
    :func:`collapsarr.library.service.resolve_tracked`); the Settings-page
    toggle exposing it is a later ticket.

    ``log_level`` (COL-130) uses the :data:`_UNSET` sentinel, like
    ``language_allow_list``: passing ``None`` explicitly clears a persisted
    override (falling back to ``COLLAPSARR_LOG_LEVEL`` at the next boot, and
    live immediately -- see :func:`collapsarr.logging_setup.apply_log_level`),
    while omitting the argument leaves the stored value untouched. A
    non-``None`` value is validated against
    :data:`~collapsarr.settings.models.LOG_LEVELS`, raising
    :class:`ValueError` otherwise. This function only persists the value --
    it does not itself touch the running logger; see the module docstring.

    ``default_audio_language``/``default_audio_channel_tier`` (COL-151) use
    the :data:`_UNSET` sentinel, like ``log_level``: passing ``None``
    explicitly clears a configured Preferred Default Audio preference field,
    while omitting the argument leaves the stored value untouched.
    ``default_audio_channel_tier`` is stored as its
    :class:`~collapsarr.downmix.targets.DownmixTarget` ``.value`` -- there is
    no separate validation step needed since the parameter is already typed
    to the enum, so an invalid tier can't reach this function at all (the
    HTTP boundary, :mod:`collapsarr.settings.routes`, also types the field as
    the enum for the same reason).

    ``auto_set_default_audio`` (COL-151) follows the same "only change what's
    passed" rule as every other boolean field here.
    """
    settings = get_global_settings(session)

    if enabled_targets is not None:
        settings.enabled_targets = _encode_targets(enabled_targets)
    if not isinstance(language_allow_list, _Unset):
        settings.language_allow_list = _encode_languages(language_allow_list)
    if stereo_codec is not None:
        settings.stereo_codec = stereo_codec
    if not isinstance(stereo_bitrate_kbps, _Unset):
        settings.stereo_bitrate_kbps = stereo_bitrate_kbps
    if surround_codec is not None:
        settings.surround_codec = surround_codec
    if not isinstance(surround_bitrate_kbps, _Unset):
        settings.surround_bitrate_kbps = surround_bitrate_kbps
    if concurrency_limit is not None:
        settings.concurrency_limit = concurrency_limit
    if ui_auth_enabled is not None:
        settings.ui_auth_enabled = ui_auth_enabled
    if not isinstance(auth_username, _Unset):
        settings.auth_username = auth_username
    if not isinstance(password, _Unset):
        settings.auth_password_hash = None if password is None else hash_password(password)
    if auth_method is not None:
        settings.auth_method = auth_method
    if auth_required is not None:
        settings.auth_required = auth_required
    if backup_interval_days is not None:
        settings.backup_interval_days = backup_interval_days
    if backup_retention_days is not None:
        settings.backup_retention_days = backup_retention_days
    if disk_space_warning_percent is not None:
        settings.disk_space_warning_percent = disk_space_warning_percent
    if disk_space_error_percent is not None:
        settings.disk_space_error_percent = disk_space_error_percent
    if update_channel is not None:
        if update_channel not in (UPDATE_CHANNEL_STABLE, UPDATE_CHANNEL_BETA):
            raise ValueError(
                "update_channel must be one of "
                f"{UPDATE_CHANNEL_STABLE!r}/{UPDATE_CHANNEL_BETA!r}; got {update_channel!r}"
            )
        settings.update_channel = update_channel
    if default_tracked is not None:
        settings.default_tracked = default_tracked
    if not isinstance(log_level, _Unset):
        if log_level is not None and log_level not in LOG_LEVELS:
            raise ValueError(f"log_level must be one of {LOG_LEVELS!r}; got {log_level!r}")
        settings.log_level = log_level
    if not isinstance(default_audio_language, _Unset):
        settings.default_audio_language = default_audio_language
    if not isinstance(default_audio_channel_tier, _Unset):
        settings.default_audio_channel_tier = (
            default_audio_channel_tier.value if default_audio_channel_tier is not None else None
        )
    if auto_set_default_audio is not None:
        settings.auto_set_default_audio = auto_set_default_audio

    session.commit()
    session.refresh(settings)
    return settings


def rotate_session_secret(session: Session) -> GlobalSettings:
    """Mint a fresh session-signing secret and persist it (COL-55).

    Uses the same generator (:func:`~collapsarr.settings.models.
    generate_session_secret`) COL-49 used for the initial mint. Every
    signed-cookie session issued under the *previous* secret fails to unsign
    against the new one, so this is the persistence half of "log out
    everywhere" -- the caller (:mod:`collapsarr.auth.routes`) still has to
    push the fresh value into the running process's cached secret (see
    :func:`collapsarr.auth.session.sync_cached_secret`), since
    :class:`~collapsarr.auth.session.SessionMiddleware` caches it on
    ``app.state`` for the life of the process rather than re-reading the DB
    on every request.
    """
    settings = get_global_settings(session)
    settings.session_secret = generate_session_secret()
    session.commit()
    session.refresh(settings)
    return settings


def verify_auth_password(session: Session, password: str) -> bool:
    """Return whether ``password`` matches the stored UI credential.

    Reads the singleton row's ``auth_password_hash`` and checks the candidate
    against it in constant time
    (:func:`collapsarr.settings.passwords.verify_password`). Returns ``False``
    when no credential has been set yet (``auth_password_hash`` is ``None``), so
    an unconfigured install never accepts a login.
    """
    settings = get_global_settings(session)
    if settings.auth_password_hash is None:
        return False
    return verify_password(password, settings.auth_password_hash)


def as_downmix_settings(settings: GlobalSettings) -> DownmixSettings:
    """Adapt a persisted :class:`GlobalSettings` row into a :class:`DownmixSettings`.

    The shape :mod:`collapsarr.downmix.pipeline` (and, eventually, the job
    queue) consumes -- decoding the comma-joined ``enabled_targets``/
    ``language_allow_list`` columns back into the ``frozenset`` forms
    :class:`~collapsarr.downmix.targets.DownmixSettings` expects.
    """
    return DownmixSettings(
        enabled_targets=_decode_targets(settings.enabled_targets),
        language_allow_list=_decode_languages(settings.language_allow_list),
        stereo_codec=settings.stereo_codec,
        stereo_bitrate_kbps=settings.stereo_bitrate_kbps,
        surround_codec=settings.surround_codec,
        surround_bitrate_kbps=settings.surround_bitrate_kbps,
    )
