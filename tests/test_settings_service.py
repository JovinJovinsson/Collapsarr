"""Tests for persisted global settings: defaults, read, update (COL-24).

Uses the shared ``session`` fixture (a schema-initialised DB session -- see
``conftest.py``), matching the pattern in ``test_arr_service.py`` and
``test_jobs_history.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, create_session_factory
from collapsarr.downmix.targets import DownmixSettings, DownmixTarget
from collapsarr.migrations import upgrade_to_head
from collapsarr.settings.models import (
    AUTH_METHOD_BASIC,
    AUTH_METHOD_FORMS,
    AUTH_REQUIRED_ENABLED,
    AUTH_REQUIRED_LOCAL_BYPASS,
    DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES,
    LOG_LEVEL_DEBUG,
    LOG_LEVEL_INFO,
    UPDATE_CHANNEL_BETA,
    UPDATE_CHANNEL_STABLE,
    GlobalSettings,
)
from collapsarr.settings.service import (
    _default_update_channel,
    as_downmix_settings,
    get_global_settings,
    rotate_session_secret,
    update_global_settings,
    verify_auth_password,
)


def _fresh_session(settings: Settings) -> Session:
    """Build a schema-initialised session for a standalone Settings/database."""
    engine = create_engine_from_settings(settings)
    upgrade_to_head(settings)
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


def test_get_global_settings_backup_schedule_defaults(session: Session) -> None:
    """COL-66: a fresh row defaults to a 7-day interval and 28-day retention."""
    settings = get_global_settings(session)

    assert settings.backup_interval_days == 7
    assert settings.backup_retention_days == 28


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
# Auto-generated session secret (COL-49).
# ---------------------------------------------------------------------------


def test_get_global_settings_generates_a_session_secret_on_first_run(session: Session) -> None:
    settings = get_global_settings(session)

    # 64-char lowercase hex (secrets.token_hex(32)).
    assert settings.session_secret is not None
    assert len(settings.session_secret) == 64
    assert all(char in "0123456789abcdef" for char in settings.session_secret)


def test_session_secret_is_stable_across_reads(session: Session) -> None:
    first = get_global_settings(session).session_secret

    assert get_global_settings(session).session_secret == first


def test_session_secret_survives_an_unrelated_update(session: Session) -> None:
    original = get_global_settings(session).session_secret

    updated = update_global_settings(session, concurrency_limit=4)

    assert updated.session_secret == original


def test_session_secret_is_backfilled_for_a_row_that_predates_the_column(
    session: Session,
) -> None:
    """A pre-existing row left NULL by schema-ensure gets a secret minted once."""
    get_global_settings(session)
    # Simulate the state right after schema-ensure adds the nullable column to
    # an existing install: the row exists but has no secret yet.
    session.execute(
        text("UPDATE global_settings SET session_secret = NULL WHERE id = 1")
    )
    session.commit()

    backfilled = get_global_settings(session).session_secret
    assert backfilled is not None
    assert len(backfilled) == 64
    # Stable thereafter -- not regenerated on the next read.
    assert get_global_settings(session).session_secret == backfilled


def test_session_secret_is_unique_per_database(settings: Settings, tmp_path: Path) -> None:
    first = get_global_settings(session=_fresh_session(settings)).session_secret

    other_settings = Settings(database_path=str(tmp_path / "other.db"))
    second = get_global_settings(session=_fresh_session(other_settings)).session_secret

    assert first != second


# ---------------------------------------------------------------------------
# Rotating the session secret (COL-55, "log out everywhere").
# ---------------------------------------------------------------------------


def test_rotate_session_secret_mints_a_new_value(session: Session) -> None:
    original = get_global_settings(session).session_secret

    rotated = rotate_session_secret(session)

    assert rotated.session_secret is not None
    assert rotated.session_secret != original
    assert len(rotated.session_secret) == 64
    assert all(char in "0123456789abcdef" for char in rotated.session_secret)


def test_rotate_session_secret_persists_across_reads(session: Session) -> None:
    rotated = rotate_session_secret(session)

    assert get_global_settings(session).session_secret == rotated.session_secret


def test_rotate_session_secret_leaves_the_credential_untouched(session: Session) -> None:
    update_global_settings(session, auth_username="operator", password="hunter2000")
    original_hash = get_global_settings(session).auth_password_hash

    rotate_session_secret(session)

    assert get_global_settings(session).auth_password_hash == original_hash


# ---------------------------------------------------------------------------
# Auth credential defaults (COL-49).
# ---------------------------------------------------------------------------


def test_auth_credential_defaults(session: Session) -> None:
    settings = get_global_settings(session)

    # No credential set on a fresh install.
    assert settings.auth_username is None
    assert settings.auth_password_hash is None
    # Sensible, secure defaults for the enum-like columns. auth_required
    # defaults to local_bypass, not enabled (COL-51): a fresh install stays
    # frictionless for a local caller.
    assert settings.auth_method == AUTH_METHOD_FORMS
    assert settings.auth_required == AUTH_REQUIRED_LOCAL_BYPASS


# ---------------------------------------------------------------------------
# Password set / verify (COL-49).
# ---------------------------------------------------------------------------


def test_set_password_hashes_and_verifies(session: Session) -> None:
    update_global_settings(session, auth_username="operator", password="s3cr3t-pw")

    settings = get_global_settings(session)
    assert settings.auth_username == "operator"
    # Never the plaintext; the stored form carries scheme, iterations, salt.
    assert settings.auth_password_hash is not None
    assert "s3cr3t-pw" not in settings.auth_password_hash
    scheme, iterations, salt_hex, digest_hex = settings.auth_password_hash.split("$")
    assert scheme == "pbkdf2_sha512"
    assert int(iterations) >= 1
    assert salt_hex and digest_hex

    assert verify_auth_password(session, "s3cr3t-pw") is True


def test_verify_rejects_a_wrong_password(session: Session) -> None:
    update_global_settings(session, password="correct-horse")

    assert verify_auth_password(session, "battery-staple") is False


def test_verify_returns_false_when_no_credential_is_set(session: Session) -> None:
    get_global_settings(session)  # seed defaults, no password

    assert verify_auth_password(session, "anything") is False


def test_password_hash_uses_a_random_salt_per_set(session: Session) -> None:
    update_global_settings(session, password="same-password")
    first_hash = get_global_settings(session).auth_password_hash

    update_global_settings(session, password="same-password")
    second_hash = get_global_settings(session).auth_password_hash

    # Different salts -> different encodings, yet both verify.
    assert first_hash != second_hash
    assert verify_auth_password(session, "same-password") is True


def test_setting_a_new_password_replaces_the_old_one(session: Session) -> None:
    update_global_settings(session, password="old-password")
    update_global_settings(session, password="new-password")

    assert verify_auth_password(session, "new-password") is True
    assert verify_auth_password(session, "old-password") is False


def test_password_none_clears_the_credential(session: Session) -> None:
    update_global_settings(session, password="temp-pw")

    cleared = update_global_settings(session, password=None)

    assert cleared.auth_password_hash is None
    assert verify_auth_password(session, "temp-pw") is False


def test_update_auth_method_and_required(session: Session) -> None:
    updated = update_global_settings(
        session,
        auth_method=AUTH_METHOD_BASIC,
        auth_required=AUTH_REQUIRED_LOCAL_BYPASS,
    )

    assert updated.auth_method == AUTH_METHOD_BASIC
    assert updated.auth_required == AUTH_REQUIRED_LOCAL_BYPASS


def test_auth_required_is_switchable_back_to_enabled(session: Session) -> None:
    """COL-51: the mode must be switchable both ways, not just away from the
    default -- e.g. an install behind a reverse proxy opting back into
    ``enabled`` (see the README's Authentication section)."""
    update_global_settings(session, auth_required=AUTH_REQUIRED_LOCAL_BYPASS)

    updated = update_global_settings(session, auth_required=AUTH_REQUIRED_ENABLED)

    assert updated.auth_required == AUTH_REQUIRED_ENABLED


def test_update_omitting_password_leaves_credential_untouched(session: Session) -> None:
    update_global_settings(session, password="keep-me")

    update_global_settings(session, concurrency_limit=2)

    assert verify_auth_password(session, "keep-me") is True


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
# Backup schedule (COL-66): interval + retention days.
# ---------------------------------------------------------------------------


def test_update_global_settings_updates_backup_interval_and_retention(session: Session) -> None:
    updated = update_global_settings(
        session, backup_interval_days=3, backup_retention_days=14
    )

    assert updated.backup_interval_days == 3
    assert updated.backup_retention_days == 14


def test_update_global_settings_omitting_backup_schedule_leaves_it_untouched(
    session: Session,
) -> None:
    update_global_settings(session, backup_interval_days=10, backup_retention_days=40)

    unchanged = update_global_settings(session, concurrency_limit=2)

    assert unchanged.backup_interval_days == 10
    assert unchanged.backup_retention_days == 40


def test_update_global_settings_backup_schedule_persists_across_a_fresh_read(
    session: Session,
) -> None:
    update_global_settings(session, backup_interval_days=1, backup_retention_days=7)

    reread = get_global_settings(session)

    assert reread.backup_interval_days == 1
    assert reread.backup_retention_days == 7


# ---------------------------------------------------------------------------
# Disk-space thresholds (COL-79): warning + error free-space percentages.
# ---------------------------------------------------------------------------


def test_get_global_settings_disk_space_threshold_defaults(session: Session) -> None:
    """COL-79: a fresh row defaults to a 5% warning / 2% error threshold."""
    settings = get_global_settings(session)

    assert settings.disk_space_warning_percent == 5.0
    assert settings.disk_space_error_percent == 2.0


def test_update_global_settings_updates_disk_space_thresholds(session: Session) -> None:
    updated = update_global_settings(
        session, disk_space_warning_percent=10.0, disk_space_error_percent=3.5
    )

    assert updated.disk_space_warning_percent == 10.0
    assert updated.disk_space_error_percent == 3.5


def test_update_global_settings_omitting_disk_space_thresholds_leaves_them_untouched(
    session: Session,
) -> None:
    update_global_settings(session, disk_space_warning_percent=8.0, disk_space_error_percent=4.0)

    unchanged = update_global_settings(session, concurrency_limit=2)

    assert unchanged.disk_space_warning_percent == 8.0
    assert unchanged.disk_space_error_percent == 4.0


def test_update_global_settings_disk_space_thresholds_persist_across_a_fresh_read(
    session: Session,
) -> None:
    update_global_settings(session, disk_space_warning_percent=6.0, disk_space_error_percent=1.0)

    reread = get_global_settings(session)

    assert reread.disk_space_warning_percent == 6.0
    assert reread.disk_space_error_percent == 1.0


# ---------------------------------------------------------------------------
# Update channel (COL-88).
# ---------------------------------------------------------------------------


def test_default_update_channel_is_stable_for_a_plain_version() -> None:
    assert _default_update_channel("0.1.0") == UPDATE_CHANNEL_STABLE


def test_default_update_channel_is_beta_for_a_beta_build_version() -> None:
    assert _default_update_channel("0.2.1.0007+beta") == UPDATE_CHANNEL_BETA


def test_get_global_settings_defaults_update_channel_to_stable(session: Session) -> None:
    settings = get_global_settings(session)

    assert settings.update_channel == UPDATE_CHANNEL_STABLE


def test_get_global_settings_defaults_update_channel_to_beta_for_a_beta_build(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COL-88/COL-96: a fresh install running a ``+beta`` build auto-defaults to beta."""
    monkeypatch.setattr("collapsarr.settings.service.__version__", "0.2.1.0007+beta")

    row = get_global_settings(session=_fresh_session(settings))

    assert row.update_channel == UPDATE_CHANNEL_BETA


def test_update_global_settings_switches_the_update_channel(session: Session) -> None:
    updated = update_global_settings(session, update_channel=UPDATE_CHANNEL_BETA)

    assert updated.update_channel == UPDATE_CHANNEL_BETA


def test_update_global_settings_update_channel_is_switchable_back_to_stable(
    session: Session,
) -> None:
    update_global_settings(session, update_channel=UPDATE_CHANNEL_BETA)

    updated = update_global_settings(session, update_channel=UPDATE_CHANNEL_STABLE)

    assert updated.update_channel == UPDATE_CHANNEL_STABLE


def test_update_global_settings_rejects_an_unknown_update_channel(session: Session) -> None:
    with pytest.raises(ValueError, match="update_channel"):
        update_global_settings(session, update_channel="nightly")


def test_update_global_settings_omitting_update_channel_leaves_it_untouched(
    session: Session,
) -> None:
    update_global_settings(session, update_channel=UPDATE_CHANNEL_BETA)

    unchanged = update_global_settings(session, concurrency_limit=3)

    assert unchanged.update_channel == UPDATE_CHANNEL_BETA


def test_update_global_settings_update_channel_persists_across_a_fresh_read(
    session: Session,
) -> None:
    update_global_settings(session, update_channel=UPDATE_CHANNEL_BETA)

    reread = get_global_settings(session)

    assert reread.update_channel == UPDATE_CHANNEL_BETA


# ---------------------------------------------------------------------------
# Log level (COL-130).
# ---------------------------------------------------------------------------


def test_get_global_settings_defaults_log_level_to_none(session: Session) -> None:
    """A fresh row's ``log_level`` is unset -- boot falls back to the env setting."""
    settings = get_global_settings(session)

    assert settings.log_level is None


def test_update_global_settings_sets_the_log_level(session: Session) -> None:
    updated = update_global_settings(session, log_level=LOG_LEVEL_DEBUG)

    assert updated.log_level == LOG_LEVEL_DEBUG


def test_update_global_settings_rejects_an_unknown_log_level(session: Session) -> None:
    with pytest.raises(ValueError, match="log_level"):
        update_global_settings(session, log_level="TRACE")


def test_update_global_settings_omitting_log_level_leaves_it_untouched(session: Session) -> None:
    update_global_settings(session, log_level=LOG_LEVEL_DEBUG)

    unchanged = update_global_settings(session, concurrency_limit=3)

    assert unchanged.log_level == LOG_LEVEL_DEBUG


def test_update_global_settings_log_level_explicit_none_clears_the_override(
    session: Session,
) -> None:
    update_global_settings(session, log_level=LOG_LEVEL_DEBUG)

    cleared = update_global_settings(session, log_level=None)

    assert cleared.log_level is None


def test_update_global_settings_log_level_persists_across_a_fresh_read(session: Session) -> None:
    update_global_settings(session, log_level=LOG_LEVEL_INFO)

    reread = get_global_settings(session)

    assert reread.log_level == LOG_LEVEL_INFO


# ---------------------------------------------------------------------------
# Preferred Default Audio (COL-151).
# ---------------------------------------------------------------------------


def test_get_global_settings_defaults_default_audio_preference_to_unset(
    session: Session,
) -> None:
    """A fresh row has no Preferred Default Audio configured, and the
    auto-set toggle is off."""
    settings = get_global_settings(session)

    assert settings.default_audio_language is None
    assert settings.default_audio_channel_tier is None
    assert settings.auto_set_default_audio is False


def test_update_global_settings_sets_the_default_audio_preference(session: Session) -> None:
    updated = update_global_settings(
        session,
        default_audio_language="eng",
        default_audio_channel_tier=DownmixTarget.FIVE_POINT_ONE,
        auto_set_default_audio=True,
    )

    assert updated.default_audio_language == "eng"
    assert updated.default_audio_channel_tier == "5.1"
    assert updated.auto_set_default_audio is True


def test_update_global_settings_omitting_default_audio_preference_leaves_it_untouched(
    session: Session,
) -> None:
    update_global_settings(
        session,
        default_audio_language="jpn",
        default_audio_channel_tier=DownmixTarget.STEREO,
        auto_set_default_audio=True,
    )

    unchanged = update_global_settings(session, concurrency_limit=3)

    assert unchanged.default_audio_language == "jpn"
    assert unchanged.default_audio_channel_tier == "stereo"
    assert unchanged.auto_set_default_audio is True


def test_update_global_settings_explicit_none_clears_default_audio_language(
    session: Session,
) -> None:
    update_global_settings(session, default_audio_language="eng")

    cleared = update_global_settings(session, default_audio_language=None)

    assert cleared.default_audio_language is None


def test_update_global_settings_explicit_none_clears_default_audio_channel_tier(
    session: Session,
) -> None:
    update_global_settings(session, default_audio_channel_tier=DownmixTarget.TWO_POINT_ONE)

    cleared = update_global_settings(session, default_audio_channel_tier=None)

    assert cleared.default_audio_channel_tier is None


def test_update_global_settings_default_audio_preference_persists_across_a_fresh_read(
    session: Session,
) -> None:
    update_global_settings(
        session,
        default_audio_language="fre",
        default_audio_channel_tier=DownmixTarget.STEREO,
        auto_set_default_audio=True,
    )

    reread = get_global_settings(session)

    assert reread.default_audio_language == "fre"
    assert reread.default_audio_channel_tier == "stereo"
    assert reread.auto_set_default_audio is True


def test_update_global_settings_auto_set_default_audio_is_switchable_back_off(
    session: Session,
) -> None:
    update_global_settings(session, auto_set_default_audio=True)

    updated = update_global_settings(session, auto_set_default_audio=False)

    assert updated.auto_set_default_audio is False


# ---------------------------------------------------------------------------
# Recently-Processed Window (COL-167).
# ---------------------------------------------------------------------------


def test_get_global_settings_recently_processed_window_default(session: Session) -> None:
    """COL-167: a fresh row defaults to a 360-minute (6h) dedup cooldown."""
    settings = get_global_settings(session)

    assert settings.recently_processed_window_minutes == DEFAULT_RECENTLY_PROCESSED_WINDOW_MINUTES


def test_update_global_settings_updates_recently_processed_window(session: Session) -> None:
    updated = update_global_settings(session, recently_processed_window_minutes=90)

    assert updated.recently_processed_window_minutes == 90


def test_update_global_settings_recently_processed_window_accepts_zero(session: Session) -> None:
    """COL-167: 0 is a valid value meaning 'no cooldown', not 'unset'."""
    updated = update_global_settings(session, recently_processed_window_minutes=0)

    assert updated.recently_processed_window_minutes == 0


def test_update_global_settings_rejects_a_negative_recently_processed_window(
    session: Session,
) -> None:
    with pytest.raises(ValueError, match="recently_processed_window_minutes"):
        update_global_settings(session, recently_processed_window_minutes=-1)


def test_update_global_settings_omitting_recently_processed_window_leaves_it_untouched(
    session: Session,
) -> None:
    update_global_settings(session, recently_processed_window_minutes=45)

    unchanged = update_global_settings(session, concurrency_limit=3)

    assert unchanged.recently_processed_window_minutes == 45


def test_update_global_settings_recently_processed_window_persists_across_a_fresh_read(
    session: Session,
) -> None:
    update_global_settings(session, recently_processed_window_minutes=15)

    reread = get_global_settings(session)

    assert reread.recently_processed_window_minutes == 15


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
