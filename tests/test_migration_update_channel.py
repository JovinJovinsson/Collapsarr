"""Tests for the ``update_channel`` + ``update_check_state`` migration (COL-86).

Mirrors ``test_migration_adoption.py``'s / ``test_migration_backup.py``'s
"post-baseline delta" idiom: build a database at the migration immediately
prior to this one, insert a real pre-migration ``global_settings`` row, then
upgrade and assert the new column is backfilled and the new table appears
cleanly -- without disturbing the existing row.
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect, text

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings
from collapsarr.migrations import build_alembic_config, upgrade_to_head
from collapsarr.settings.models import UPDATE_CHANNEL_STABLE

#: The revision immediately prior to COL-86's migration (COL-82's
#: ``health_check_state.dismissed_at`` column).
_PRIOR_REVISION = "44a814a6e938"


def _create_pre_migration_global_settings_row(settings: Settings) -> None:
    """Stamp the DB at :data:`_PRIOR_REVISION` and insert a real existing row.

    Models an already-deployed install about to take the COL-86 update: its
    ``global_settings`` row predates the ``update_channel`` column entirely.
    """
    config = build_alembic_config(settings)
    command.upgrade(config, _PRIOR_REVISION)

    engine = create_engine_from_settings(settings)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO global_settings "
                    "(id, api_key, enabled_targets, stereo_codec, surround_codec, "
                    " concurrency_limit, ui_auth_enabled, auth_method, auth_required, "
                    " backup_interval_days, backup_retention_days, "
                    " disk_space_warning_percent, disk_space_error_percent, "
                    " created_at, updated_at) "
                    "VALUES (1, 'existingkey', 'stereo', 'aac', 'ac3', 1, 0, "
                    " 'forms', 'local_bypass', 7, 28, 5.0, 2.0, "
                    " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                )
            )
    finally:
        engine.dispose()


def test_migration_backfills_existing_row_to_stable(settings: Settings) -> None:
    """An existing install's row gets ``update_channel = 'stable'`` on upgrade."""
    _create_pre_migration_global_settings_row(settings)

    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            channel = connection.execute(
                text("SELECT update_channel FROM global_settings WHERE id = 1")
            ).scalar_one()
            # The pre-existing row survived the migration untouched otherwise.
            api_key = connection.execute(
                text("SELECT api_key FROM global_settings WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert channel == UPDATE_CHANNEL_STABLE
    assert api_key == "existingkey"


def test_migration_creates_update_check_state_table_cleanly(settings: Settings) -> None:
    """``update_check_state`` appears, empty, on top of an existing database."""
    _create_pre_migration_global_settings_row(settings)

    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("update_check_state")
        columns = {c["name"] for c in inspector.get_columns("update_check_state")}
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM update_check_state")
            ).scalar_one()
    finally:
        engine.dispose()

    assert columns == {
        "id",
        "channel",
        "latest_tag",
        "latest_version_label",
        "changelog",
        "published_at",
        "checked_at",
        "dismissed_at",
    }
    assert count == 0  # the scheduler's first tick inserts the singleton row, not this migration


def test_fresh_install_has_update_channel_and_table(settings: Settings) -> None:
    """A brand-new install's schema has both, and a freshly-created row defaults to stable."""
    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("update_check_state")
        assert "update_channel" in {c["name"] for c in inspector.get_columns("global_settings")}
        with engine.begin() as connection:
            # A fresh install has no global_settings row yet -- insert one the
            # way collapsarr.settings.service.get_global_settings does (every
            # column but update_channel explicit), relying on the DB-side
            # server_default to prove the backfill default, not the ORM's
            # Python-side default.
            connection.execute(
                text(
                    "INSERT INTO global_settings "
                    "(id, api_key, enabled_targets, stereo_codec, surround_codec, "
                    " concurrency_limit, ui_auth_enabled, created_at, updated_at) "
                    "VALUES (1, 'freshkey', 'stereo', 'aac', 'ac3', 1, 0, "
                    " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                )
            )
            channel = connection.execute(
                text("SELECT update_channel FROM global_settings WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert channel == UPDATE_CHANNEL_STABLE
