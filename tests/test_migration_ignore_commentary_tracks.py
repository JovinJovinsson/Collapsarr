"""Tests for the ``ignore_commentary_tracks`` migration (COL-244).

Mirrors ``test_migration_auto_processing_paused.py``'s "post-baseline delta"
idiom: build a database at the migration immediately prior to this one,
insert a real pre-migration ``global_settings`` row, then upgrade and assert
the new column appears cleanly, backfilled to ``True`` -- without disturbing
the existing row.
"""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect, text

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings
from collapsarr.migrations import build_alembic_config, upgrade_to_head

#: The revision immediately prior to COL-244's migration (COL-236's
#: job-history ``expected_stream_count`` column).
_PRIOR_REVISION = "35a5abb7e183"


def _create_pre_migration_global_settings_row(settings: Settings) -> None:
    """Stamp the DB at :data:`_PRIOR_REVISION` and insert a real existing row.

    Models an already-deployed install about to take the COL-244 update: its
    ``global_settings`` row predates the ``ignore_commentary_tracks`` column
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


def test_migration_backfills_existing_row_to_true(settings: Settings) -> None:
    """An existing install's row gets ``ignore_commentary_tracks = 1`` (ignored by default)."""
    _create_pre_migration_global_settings_row(settings)

    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        with engine.connect() as connection:
            ignored = connection.execute(
                text("SELECT ignore_commentary_tracks FROM global_settings WHERE id = 1")
            ).scalar_one()
            # The pre-existing row survived the migration untouched otherwise.
            api_key = connection.execute(
                text("SELECT api_key FROM global_settings WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert ignored == 1
    assert api_key == "existingkey"


def test_fresh_install_has_ignore_commentary_tracks_column_defaulting_to_true(
    settings: Settings,
) -> None:
    """A brand-new install's schema has the column, and a fresh row defaults to on."""
    upgrade_to_head(settings)

    engine = create_engine_from_settings(settings)
    try:
        inspector = inspect(engine)
        assert "ignore_commentary_tracks" in {
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
            ignored = connection.execute(
                text("SELECT ignore_commentary_tracks FROM global_settings WHERE id = 1")
            ).scalar_one()
    finally:
        engine.dispose()

    assert ignored == 1
