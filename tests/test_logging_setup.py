"""Tests for collapsarr.logging_setup (COL-128)."""

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest

from collapsarr.config import Settings
from collapsarr.logging_setup import LOGGER_NAME, configure_logging, logs_dir
from collapsarr.main import create_app


@pytest.fixture(autouse=True)
def _reset_collapsarr_logger() -> Iterator[None]:
    """Snapshot/restore the real ``collapsarr`` logger around each test.

    ``configure_logging`` mutates process-global logging state (there is only
    one ``logging.getLogger("collapsarr")``), so a test that leaves stale
    handlers pointing at its own ``tmp_path`` would otherwise leak into
    whichever test runs next in this process.
    """
    logger = logging.getLogger(LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_filters = list(logger.filters)
    original_level = logger.level
    original_propagate = logger.propagate
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for existing_filter in list(logger.filters):
        logger.removeFilter(existing_filter)
    for handler in original_handlers:
        logger.addHandler(handler)
    for existing_filter in original_filters:
        logger.addFilter(existing_filter)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def test_configure_logging_attaches_stdout_and_rotating_file_handlers(
    settings: Settings,
) -> None:
    configure_logging(settings)
    logger = logging.getLogger(LOGGER_NAME)

    stream_handlers = [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    file_handlers = [
        h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]

    assert len(stream_handlers) == 1
    assert len(file_handlers) == 1
    assert logger.propagate is False


def test_configure_logging_creates_logs_dir(settings: Settings) -> None:
    target = logs_dir(settings)
    assert not target.exists()

    configure_logging(settings)

    assert target.is_dir()
    assert (target / "collapsarr.log").exists()


def test_log_call_reaches_both_stdout_and_file(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    logger = logging.getLogger("collapsarr.some.module")

    logger.info("hello from a submodule")

    captured = capsys.readouterr()
    assert "hello from a submodule" in captured.out

    log_file = logs_dir(settings) / "collapsarr.log"
    assert "hello from a submodule" in log_file.read_text()


def test_backup_count_is_6_at_info_and_51_at_debug(settings: Settings) -> None:
    info_settings = settings.model_copy(update={"log_level": "INFO"})
    configure_logging(info_settings)
    logger = logging.getLogger(LOGGER_NAME)
    file_handler = next(
        h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert file_handler.backupCount == 6
    assert file_handler.maxBytes == 1_000_000

    # Re-configuring with a different resolved level updates the backup
    # count live (AC), without accumulating a second file handler.
    debug_settings = settings.model_copy(update={"log_level": "DEBUG"})
    configure_logging(debug_settings)
    logger = logging.getLogger(LOGGER_NAME)
    file_handlers = [
        h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].backupCount == 51


def test_configure_logging_is_idempotent(settings: Settings) -> None:
    configure_logging(settings)
    configure_logging(settings)
    configure_logging(settings)

    logger = logging.getLogger(LOGGER_NAME)
    assert len(logger.handlers) == 2
    for handler in logger.handlers:
        assert len(handler.filters) == 1


def test_configure_logging_does_not_leak_output_across_settings(
    settings: Settings, tmp_path: Path
) -> None:
    configure_logging(settings)
    logger = logging.getLogger("collapsarr.leak.check")
    logger.info("first settings")

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_settings = Settings(
        database_path=str(other_dir / "collapsarr.db"), data_dir=str(other_dir)
    )
    configure_logging(other_settings)
    logger.info("second settings")

    first_log = logs_dir(settings) / "collapsarr.log"
    second_log = logs_dir(other_settings) / "collapsarr.log"

    assert "first settings" in first_log.read_text()
    assert "second settings" not in first_log.read_text()
    assert "second settings" in second_log.read_text()
    assert "first settings" not in second_log.read_text()


def test_redaction_scrubs_apikey_query_value(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    logger = logging.getLogger("collapsarr.arr.catalog")

    logger.error(
        "GET failed: https://sonarr.example/api/v3/system/status?apikey=abcdef0123456789"
    )

    out = capsys.readouterr().out
    assert "apikey=***" in out
    assert "abcdef0123456789" not in out
    assert "GET failed: https://sonarr.example/api/v3/system/status" in out


def test_redaction_scrubs_discord_webhook_token(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    logger = logging.getLogger("collapsarr.notify.dispatch")

    logger.error(
        "webhook post failed: "
        "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWx"
    )

    out = capsys.readouterr().out
    assert "https://discord.com/api/webhooks/123456789012345678/***" in out
    assert "AbCdEfGhIjKlMnOpQrStUvWx" not in out


def test_redaction_scrubs_authorization_header_value(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    logger = logging.getLogger("collapsarr.some.client")

    logger.warning("request headers: Authorization: Bearer super-secret-token-value")

    out = capsys.readouterr().out
    assert "Authorization: ***" in out
    assert "super-secret-token-value" not in out


def test_redaction_leaves_non_secret_content_unchanged(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    logger = logging.getLogger("collapsarr.jobs.scheduler")

    logger.info("job 42 completed in 3.5s for movie 'Some Title'")

    out = capsys.readouterr().out
    assert "job 42 completed in 3.5s for movie 'Some Title'" in out


def test_create_app_configures_logging_with_its_own_settings(settings: Settings) -> None:
    app = create_app(settings=settings)
    assert app is not None

    logger = logging.getLogger(LOGGER_NAME)
    file_handler = next(
        h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert Path(file_handler.baseFilename) == logs_dir(settings) / "collapsarr.log"
