"""Tests for the ``log_level`` migration (COL-130).

Mirrors ``test_migration_update_channel.py``'s "post-baseline delta" idiom:
build a database at the migration immediately prior to this one, insert a real
pre-migration ``global_settings`` row, then upgrade and assert the new column
appears cleanly, backfilled to ``NULL`` -- without disturbing the existing row.
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect, text

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings
from collapsarr.migrations import build_alembic_config, upgrade_to_head

#: The revision immediately prior to COL-130's migration (COL-101's
#: ``tracked_media_files`` library-node bridge columns).
_PRIOR_REVISION = "c6e21d7b6185"


def _create_pre_migration_global_settings_row(settings: Settings) -> None:
    """Stamp the DB at :data:`_PRIOR_REVISION` and insert a real existing row.

    Models an already-deployed install about to take the COL-130 update: its
    ``global_settings`` row predates the ``log_level`` column entirely.
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
                    " disk_space_warning_percent, disk_space_error_percent, update_channel, "
                    " created_at, updated_at) "
                    "VALUES (1, 'existingkey', 'stereo', 'aac', 'ac3', 1, 0, "
                    " 'forms', 'local_bypass', 7, 28, 5.0, 2.0, 'stable', "
                    " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                )
            )
    finally:
        engine.dispose()


def test_migration_backfills_existing_row_to_null(settings: Settings) -> None:
    """An existing install's row gets ``log_level = NULL`` (not a concrete level)."""
    _create_pre_migration_global_settings_row(settings)

    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            level = connection.execute(
                text("SELECT log_level FROM global_settings WHERE id = 1")
            ).scalar_one_or_none()
            # The pre-existing row survived the migration untouched otherwise.
            api_key = connection.execute(
                text("SELECT api_key FROM global_settings WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert level is None
    assert api_key == "existingkey"


def test_fresh_install_has_log_level_column_defaulting_to_null(settings: Settings) -> None:
    """A brand-new install's schema has the column, and a fresh row defaults to unset."""
    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        assert "log_level" in {c["name"] for c in inspector.get_columns("global_settings")}
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO global_settings "
                    "(id, api_key, enabled_targets, stereo_codec, surround_codec, "
                    " concurrency_limit, ui_auth_enabled, created_at, updated_at) "
                    "VALUES (1, 'freshkey', 'stereo', 'aac', 'ac3', 1, 0, "
                    " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                )
            )
            level = connection.execute(
                text("SELECT log_level FROM global_settings WHERE id = 1")
            ).scalar_one_or_none()
    finally:
        engine.dispose()

    assert level is None
