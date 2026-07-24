"""Tests for the idempotent startup schema-ensure step (COL-48).

``init_db`` (see ``collapsarr/database.py``) runs ``Base.metadata.create_all``
then ``ensure_schema``, which diffs each mapped table's model columns against
the database's actual columns and issues ``ALTER TABLE ... ADD COLUMN`` for
anything missing. These tests simulate an "older schema" database by hand
(raw DDL/DML for a real mapped table's SQL, minus one column it has since
gained) -- the same ``settings``/engine-construction pattern the ``session``
fixture in ``conftest.py`` uses -- then run ``init_db`` against it and assert
the column appears, existing data survives, and a second run is a no-op.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from collapsarr.config import Settings
from collapsarr.database import create_engine_from_settings, ensure_schema, init_db
from collapsarr.notify.models import NotifierConfig


def _create_older_notifier_config_schema(settings: Settings) -> None:
    """Create ``notifier_config`` as it looked before ``discord_webhook_url``.

    Raw DDL/DML rather than the ORM so the table is created exactly as
    requested -- missing one real, currently-mapped column
    (``discord_webhook_url`` on :class:`~collapsarr.notify.models.NotifierConfig`)
    -- independent of whatever ``init_db``/``ensure_schema`` might otherwise do.
    """
    engine = create_engine_from_settings(settings)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE notifier_config (
                    id INTEGER NOT NULL PRIMARY KEY,
                    webhook_url VARCHAR(2048),
                    webhook_enabled BOOLEAN NOT NULL,
                    discord_enabled BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notifier_config
                    (id, webhook_url, webhook_enabled, discord_enabled, created_at, updated_at)
                VALUES
                    (1, 'https://example.com/hook', 1, 0,
                     '2026-01-01T00:00:00', '2026-01-01T00:00:00')
                """
            )
        )
    engine.dispose()


def test_init_db_adds_column_missing_from_older_schema(settings: Settings) -> None:
    """A DB at an older schema (missing a mapped column) gets it added on init."""
    _create_older_notifier_config_schema(settings)

    engine = create_engine_from_settings(settings)
    inspector = inspect(engine)
    columns_before = {col["name"] for col in inspector.get_columns("notifier_config")}
    assert "discord_webhook_url" not in columns_before

    init_db(engine)

    inspector = inspect(engine)
    columns_after = {col["name"] for col in inspector.get_columns("notifier_config")}
    assert "discord_webhook_url" in columns_after
    # Nothing else was touched: still exactly the older columns plus the new one.
    assert columns_after == columns_before | {"discord_webhook_url"}

    engine.dispose()


def test_init_db_preserves_existing_data_when_adding_a_column(settings: Settings) -> None:
    """Adding the missing column must not disturb the pre-existing row."""
    _create_older_notifier_config_schema(settings)

    engine = create_engine_from_settings(settings)
    init_db(engine)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, webhook_url, webhook_enabled, discord_enabled, discord_webhook_url "
                "FROM notifier_config WHERE id = 1"
            )
        ).one()

    assert row.webhook_url == "https://example.com/hook"
    assert row.webhook_enabled == 1
    assert row.discord_enabled == 0
    # The backfilled column has no data for the pre-existing row.
    assert row.discord_webhook_url is None

    engine.dispose()


def test_init_db_rerun_is_idempotent(settings: Settings) -> None:
    """Running init_db (and therefore ensure_schema) again is a no-op."""
    _create_older_notifier_config_schema(settings)

    engine = create_engine_from_settings(settings)
    init_db(engine)

    inspector = inspect(engine)
    columns_after_first_run = {col["name"] for col in inspector.get_columns("notifier_config")}

    # No error, and the schema is unchanged the second (and third) time.
    init_db(engine)
    ensure_schema(engine)

    inspector = inspect(engine)
    columns_after_rerun = {col["name"] for col in inspector.get_columns("notifier_config")}
    assert columns_after_rerun == columns_after_first_run

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT webhook_url FROM notifier_config WHERE id = 1")
        ).one()
    assert row.webhook_url == "https://example.com/hook"

    engine.dispose()


def test_init_db_boots_cleanly_against_a_fresh_database(settings: Settings) -> None:
    """A brand-new database (no tables at all) is unaffected by ensure_schema."""
    engine = create_engine_from_settings(settings)

    init_db(engine)  # fresh DB: create_all does the work, ensure_schema is a no-op

    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("notifier_config")}
    assert "discord_webhook_url" in columns
    assert {c.name for c in NotifierConfig.__table__.columns} <= columns

    # Re-running against a fresh, fully-up-to-date DB is also a no-op.
    init_db(engine)
    inspector = inspect(engine)
    assert {col["name"] for col in inspector.get_columns("notifier_config")} == columns

    engine.dispose()
