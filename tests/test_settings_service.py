"""Tests for persisted global settings: defaults, read, update (COL-24).

Uses the shared ``session`` fixture (a schema-initialised DB session -- see
``conftest.py``), matching the pattern in ``test_arr_service.py`` and
``test_jobs_history.py``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory, init_db
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.settings.models import GlobalSettings
from collapsarr.settings.service import (
    as_downmix_settings,
    get_global_settings,
    update_global_settings,
)


def _fresh_session(settings: Settings) -> Session:
    """Build a schema-initialised session for a standalone Settings/database."""
    engine = create_engine_from_settings(settings)
    init_db(engine)
    return create_session_factory(engine)()

# ---------------------------------------------------------------------------
# Default creation on first run.
# ---------------------------------------------------------------------------


def test_get_global_settings_creates_the_row_on_first_call(session: Session) -> None:
    assert session.scalars(select(GlobalSettings)).one_or_none() is None

    settings = get_global_settings(session)

    assert settings.id == 1
    assert session.scalars(select(GlobalSettings)).one().id == settings.id


def test_get_global_settings_defaults_match_the_prd(session: Session) -> None:
    """Stereo enabled by default (2.1/5.1 opt-in), AAC/AC3@448k, concurrency=1, auth disabled."""
    settings = get_global_settings(session)

    assert settings.enabled_targets == DownmixTarget.STEREO.value
    assert settings.language_allow_list is None
    assert settings.stereo_codec == "aac"
    assert settings.stereo_bitrate_kbps is None
    assert settings.surround_codec == "ac3"
    assert settings.surround_bitrate_kbps == 448
    assert settings.concurrency_limit == 1
    assert settings.ui_auth_enabled is False


def test_get_global_settings_does_not_duplicate_the_row_across_calls(session: Session) -> None:
    first = get_global_settings(session)
    second = get_global_settings(session)

    assert first.id == second.id
    assert session.scalars(select(GlobalSettings)).all() == [first]


# ---------------------------------------------------------------------------
# Auto-generated API key (COL-26).
# ---------------------------------------------------------------------------


def test_get_global_settings_generates_an_api_key_on_first_run(session: Session) -> None:
    settings = get_global_settings(session)

    # 32-char lowercase hex, matching the *arr API-key format.
    assert len(settings.api_key) == 32
    assert all(char in "0123456789abcdef" for char in settings.api_key)


def test_api_key_is_stable_across_reads(session: Session) -> None:
    first = get_global_settings(session).api_key

    assert get_global_settings(session).api_key == first


def test_api_key_is_unique_per_database(settings: Settings, tmp_path: Path) -> None:
    """Each fresh install mints its own key rather than a shared constant."""
    first = get_global_settings(session=_fresh_session(settings)).api_key

    other_settings = Settings(database_path=str(tmp_path / "other.db"))
    second = get_global_settings(session=_fresh_session(other_settings)).api_key

    assert first != second


# ---------------------------------------------------------------------------
# Read.
# ---------------------------------------------------------------------------


def test_get_global_settings_reads_back_a_previously_created_row(session: Session) -> None:
    created = get_global_settings(session)
    session.expunge(created)

    read_back = get_global_settings(session)

    assert read_back.id == created.id
    assert read_back.concurrency_limit == created.concurrency_limit


# ---------------------------------------------------------------------------
# Update.
# ---------------------------------------------------------------------------


def test_update_global_settings_changes_only_the_given_fields(session: Session) -> None:
    get_global_settings(session)  # seed defaults first

    updated = update_global_settings(session, concurrency_limit=3, ui_auth_enabled=True)

    assert updated.concurrency_limit == 3
    assert updated.ui_auth_enabled is True
    # Untouched fields keep their defaults.
    assert updated.enabled_targets == DownmixTarget.STEREO.value
    assert updated.stereo_codec == "aac"
    assert updated.surround_bitrate_kbps == 448


def test_update_global_settings_creates_the_row_if_absent(session: Session) -> None:
    assert session.scalars(select(GlobalSettings)).one_or_none() is None

    updated = update_global_settings(session, concurrency_limit=2)

    assert updated.id == 1
    assert updated.concurrency_limit == 2


def test_update_global_settings_persists_across_a_fresh_read(session: Session) -> None:
    update_global_settings(session, ui_auth_enabled=True)

    reread = get_global_settings(session)

    assert reread.ui_auth_enabled is True


def test_update_global_settings_encodes_enabled_targets(session: Session) -> None:
    updated = update_global_settings(
        session,
        enabled_targets=frozenset({DownmixTarget.STEREO, DownmixTarget.FIVE_POINT_ONE}),
    )

    assert updated.enabled_targets == "5.1,stereo"


def test_update_global_settings_encodes_language_allow_list(session: Session) -> None:
    updated = update_global_settings(
        session, language_allow_list=frozenset({"eng", "jpn"})
    )

    assert updated.language_allow_list == "eng,jpn"


def test_update_global_settings_explicit_none_clears_a_nullable_field(session: Session) -> None:
    update_global_settings(session, language_allow_list=frozenset({"eng"}))

    cleared = update_global_settings(session, language_allow_list=None)

    assert cleared.language_allow_list is None


def test_update_global_settings_omitted_nullable_field_stays_untouched(session: Session) -> None:
    update_global_settings(session, language_allow_list=frozenset({"eng"}))

    unchanged = update_global_settings(session, concurrency_limit=5)

    assert unchanged.language_allow_list == "eng"
    assert unchanged.concurrency_limit == 5


def test_update_global_settings_explicit_none_clears_bitrate_overrides(session: Session) -> None:
    update_global_settings(session, surround_bitrate_kbps=640)

    cleared = update_global_settings(session, surround_bitrate_kbps=None)

    assert cleared.surround_bitrate_kbps is None


# ---------------------------------------------------------------------------
# Adapting to DownmixSettings.
# ---------------------------------------------------------------------------


def test_as_downmix_settings_adapts_defaults(session: Session) -> None:
    settings = get_global_settings(session)

    downmix_settings = as_downmix_settings(settings)

    assert downmix_settings == DownmixSettings()


def test_as_downmix_settings_adapts_customised_values(session: Session) -> None:
    settings = update_global_settings(
        session,
        enabled_targets=frozenset({DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE}),
        language_allow_list=frozenset({"eng"}),
        stereo_bitrate_kbps=192,
        surround_bitrate_kbps=640,
    )

    downmix_settings = as_downmix_settings(settings)

    assert downmix_settings.enabled_targets == frozenset(
        {DownmixTarget.STEREO, DownmixTarget.TWO_POINT_ONE}
    )
    assert downmix_settings.language_allow_list == frozenset({"eng"})
    assert downmix_settings.stereo_bitrate_kbps == 192
    assert downmix_settings.surround_bitrate_kbps == 640


# ---------------------------------------------------------------------------
# Public re-exports.
# ---------------------------------------------------------------------------


def test_global_settings_importable_from_package_root() -> None:
    """Sanity check the public re-exports from collapsarr.settings."""
    from collapsarr.settings import GlobalSettings as ReexportedGlobalSettings
    from collapsarr.settings import as_downmix_settings as reexported_adapt
    from collapsarr.settings import get_global_settings as reexported_get
    from collapsarr.settings import update_global_settings as reexported_update

    assert ReexportedGlobalSettings is GlobalSettings
    assert reexported_get is get_global_settings
    assert reexported_update is update_global_settings
    assert reexported_adapt is as_downmix_settings
