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
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from collapsarr.downmix.targets import DownmixSettings, DownmixTarget

from .models import SETTINGS_ID, GlobalSettings


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


def get_global_settings(session: Session) -> GlobalSettings:
    """Return the singleton settings row, creating it with defaults if absent.

    Safe to call repeatedly and from multiple call sites (job queue,
    downmix pipeline, a future web UI) -- once created, the same row is
    always returned; it is never recreated or duplicated (enforced at the
    schema level by :class:`~collapsarr.settings.models.GlobalSettings`'s
    singleton check constraint).
    """
    settings = session.get(GlobalSettings, SETTINGS_ID)
    if settings is None:
        settings = GlobalSettings(id=SETTINGS_ID)
        session.add(settings)
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
) -> GlobalSettings:
    """Update the given fields on the settings row and return it.

    Only fields passed explicitly are changed, matching the convention
    :func:`collapsarr.arr.service.update_instance` already uses. For the
    three fields whose valid domain includes ``None`` as a meaningful value
    (``language_allow_list``, ``stereo_bitrate_kbps``,
    ``surround_bitrate_kbps``), passing ``None`` explicitly *clears* the
    stored value (e.g. removes a bitrate override) -- omitting the argument
    (the default) leaves it untouched. Creates the row with defaults first
    if it doesn't exist yet, same as :func:`get_global_settings`.
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

    session.commit()
    session.refresh(settings)
    return settings


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
