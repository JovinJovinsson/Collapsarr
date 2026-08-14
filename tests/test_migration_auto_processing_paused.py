"""Tests for the ``auto_processing_paused`` migration (COL-226, "Auto-Processing Pause").

Mirrors ``test_migration_auto_queue_paused.py``'s "post-baseline delta" idiom: build a
database at the migration immediately prior to this one, insert a real
pre-migration ``global_settings`` row, then upgrade and assert the new column
appears cleanly, backfilled to ``False`` -- without disturbing the existing
row.
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect, text

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings
from collapsarr.migrations import build_alembic_config, upgrade_to_head

#: The revision immediately prior to COL-226's migration (COL-218's
#: ``ffmpeg_path`` column).
_PRIOR_REVISION = "c4d5e6f7a8b9"


def _create_pre_migration_global_settings_row(settings: Settings) -> None:
    """Stamp the DB at :data:`_PRIOR_REVISION` and insert a real existing row.

    Models an already-deployed install about to take the COL-226 update: its
    ``global_settings`` row predates the ``auto_processing_paused`` column
    entirely.
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
                    " concurrency_limit, ui_auth_enabled, created_at, updated_at) "
                    "VALUES (1, 'existingkey', 'stereo', 'aac', 'ac3', 1, 0, "
                    " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                )
            )
    finally:
        engine.dispose()


def test_migration_backfills_existing_row_to_false(settings: Settings) -> None:
    """An existing install's row gets ``auto_processing_paused = 0`` (not paused)."""
    _create_pre_migration_global_settings_row(settings)

    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            paused = connection.execute(
                text("SELECT auto_processing_paused FROM global_settings WHERE id = 1")
            ).scalar_one()
            # The pre-existing row survived the migration untouched otherwise.
            api_key = connection.execute(
                text("SELECT api_key FROM global_settings WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert paused == 0
    assert api_key == "existingkey"


def test_fresh_install_has_auto_processing_paused_column_defaulting_to_false(
    settings: Settings,
) -> None:
    """A brand-new install's schema has the column, and a fresh row defaults to off."""
    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        assert "auto_processing_paused" in {
            c["name"] for c in inspector.get_columns("global_settings")
        }
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
            paused = connection.execute(
                text("SELECT auto_processing_paused FROM global_settings WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert paused == 0
