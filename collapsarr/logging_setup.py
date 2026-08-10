"""Logging infrastructure: rotating file + stdout, with secret redaction (COL-128).

:func:`configure_logging` attaches two handlers to the ``collapsarr`` named
logger -- the common parent every module's ``logging.getLogger(__name__)``
reports to (``collapsarr.arr.catalog``, ``collapsarr.backup.scheduler``, ...):

* A :class:`~logging.StreamHandler` to stdout, so ``docker logs``/journald
  capture keeps working exactly as before this ticket.
* A :class:`~logging.handlers.RotatingFileHandler` writing to
  ``<data_dir>/logs/collapsarr.log`` (created on startup the same way
  :func:`collapsarr.backup.service.ensure_backup_dirs` creates
  ``<data_dir>/backups``), 1 MB per file. ``backupCount`` is 51 when the
  resolved level is DEBUG, 6 otherwise (INFO/WARNING/ERROR) -- mirroring
  Radarr/Sonarr's own Info-vs-Debug/Trace retention split, adapted to
  Python's four-level set.

``collapsarr``'s ``propagate`` is set ``False``, so the true Python root
logger -- and therefore third-party libraries like ``httpx``/``sqlalchemy``
that log through it -- is left untouched.

Both handlers share one :class:`~logging.Filter` (see :class:`_RedactionFilter`)
that scrubs known secret shapes out of every record before either handler
formats it: Arr ``apikey=`` query-string values, the token segment of
Discord/webhook notifier URLs, and ``Authorization:`` header values. See
``docs/adr/0006-log-redaction-regex-scrub-not-call-site-audit.md`` for why a
regex-scrubbing filter was chosen over auditing every call site -- it is
best-effort, not a guarantee, and a novel secret shape needs a new pattern
added below.

``configure_logging`` is idempotent: it clears any handlers/filters already on
the ``collapsarr`` logger before adding new ones. Tests build a fresh
:class:`~fastapi.FastAPI` app per test via ``create_app()`` with an isolated
``tmp_path``-based :class:`~collapsarr.config.Settings` (see
``tests/conftest.py``), and ``create_app()`` calls this on every build -- so a
process running many tests back to back must never leak a handler pointing at
a previous test's (by-then-deleted) ``tmp_path``, nor fan a single log line
out across every previous test's log file.

:func:`apply_log_level` (COL-130) is the lighter-weight runtime counterpart:
given a level name (or ``None``), it updates the existing logger's effective
level and the existing rotating file handler's ``backupCount`` in place --
unlike :func:`configure_logging`, it never touches ``data_dir`` or rebuilds
handlers, so it needs no :class:`~collapsarr.config.Settings` at all. It backs
the ``GlobalSettings.log_level`` Settings -> General dropdown: a change made
through ``PUT /api/settings`` (:mod:`collapsarr.settings.routes`) takes effect
immediately, with no restart, and a value left unset (``None``) resolves to
whatever level :func:`configure_logging` most recently derived from
``COLLAPSARR_LOG_LEVEL`` (tracked in :data:`_env_level`).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

from .config import Settings

#: The common parent logger every module's ``logging.getLogger(__name__)``
#: reports to (``collapsarr.arr.catalog`` etc. are children of this).
LOGGER_NAME = "collapsarr"

#: Fixed replacement for every redacted secret (ADR 0006).
_MASK = "***"

_MAX_BYTES = 1_000_000  # 1 MB per log file, per the ticket's AC.
_BACKUP_COUNT_DEBUG = 51
_BACKUP_COUNT_DEFAULT = 6

#: Filename of the current (not-yet-rotated) log file within :func:`logs_dir`.
#: Exported so :mod:`collapsarr.system.logs` (COL-131) can locate the exact
#: same file this module's :class:`~logging.handlers.RotatingFileHandler`
#: writes, without duplicating the literal.
LOG_FILENAME = "collapsarr.log"

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_env_level: int = logging.INFO
"""The level :func:`configure_logging` most recently resolved from
``COLLAPSARR_LOG_LEVEL`` (env) -- the floor :func:`apply_log_level` falls back
to when the DB-persisted ``GlobalSettings.log_level`` override (COL-130) is
``None``. Module-level rather than threaded through every call site, matching
how the ``collapsarr`` logger itself is process-global mutable state (there is
only one ``logging.getLogger("collapsarr")``, same caveat as documented above
for handlers/filters)."""

# --- Redaction patterns (ADR 0006) ------------------------------------------
# Regex-scrub known secret shapes rather than auditing every call site. Each
# pattern captures the prefix it wants to keep (for readability/debuggability)
# in group 1 and redacts only the secret portion.

#: Sonarr/Radarr's own API convention: ``?apikey=<key>`` (or ``&apikey=``) in
#: a query string, frequently embedded verbatim in an httpx.HTTPError message.
_APIKEY_QUERY_RE = re.compile(r"(apikey=)[^&\s]+", re.IGNORECASE)

#: The token segment of a Discord (or Discord-shaped generic) webhook URL,
#: e.g. ``https://discord.com/api/webhooks/123456789012345678/<token>``.
#: Collapsarr's own generic ``webhook_url``/``discord_webhook_url`` notifier
#: config (collapsarr/notify) carries the same "opaque token after a numeric
#: id, under a /webhooks/ path" exposure shape.
_WEBHOOK_TOKEN_RE = re.compile(
    r"(https?://\S+/webhooks/\d+/)\S+",
    re.IGNORECASE,
)

#: An ``Authorization:`` header value, e.g. ``Authorization: Bearer <token>``.
#: Redacts the whole value (to end of line) rather than just the scheme, since
#: the credential-bearing portion varies by scheme (Bearer/Basic/etc.).
_AUTH_HEADER_RE = re.compile(r"(authorization:\s*).+$", re.IGNORECASE | re.MULTILINE)

_REDACTION_PATTERNS = (_APIKEY_QUERY_RE, _WEBHOOK_TOKEN_RE, _AUTH_HEADER_RE)


def _redact(message: str) -> str:
    """Scrub every known secret shape out of ``message`` (ADR 0006)."""
    for pattern in _REDACTION_PATTERNS:
        message = pattern.sub(rf"\1{_MASK}", message)
    return message


class _RedactionFilter(logging.Filter):
    """Scrub known secret shapes from every ``collapsarr`` log record.

    The *same instance* is attached to both handlers below, so the identical
    scrub rules apply to the stdout stream and the rotating file -- one place
    to extend, not two (ADR 0006). Deliberately attached per-handler rather
    than to the ``collapsarr`` logger itself: almost every real log call
    originates on a *child* logger (``collapsarr.arr.catalog`` etc.), and
    Python's ``logging`` module only runs a ``Logger``'s own filters for
    records that originate at that exact logger -- propagated records from
    children skip straight to each ancestor's *handlers* (``Handler.filter``
    runs there), never back through the ancestor ``Logger.filter``. A filter
    on the logger object would therefore silently never see the vast majority
    of ``collapsarr`` log traffic.

    Rewrites ``record.msg`` to the already-%-formatted, already-redacted
    string and clears ``record.args`` so a handler's later
    ``record.getMessage()`` call doesn't try to re-apply ``%`` formatting.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = ()
        return True


def _level_from_name(name: str) -> int:
    """Resolve a level name (``"DEBUG"``, ``"info"``, ...) to its numeric level.

    Falls back to ``INFO`` for anything :func:`logging.getLevelName` doesn't
    recognise as a level, matching :func:`_resolved_level`'s previous inline
    behaviour.
    """
    level = logging.getLevelName(name.upper())
    return level if isinstance(level, int) else logging.INFO


def _resolved_level(settings: Settings) -> int:
    """Resolve ``settings.log_level`` (env-sourced) to a numeric level."""
    return _level_from_name(settings.log_level)


def _backup_count_for(level: int) -> int:
    """51 backups at DEBUG, 6 otherwise -- see module docstring."""
    return _BACKUP_COUNT_DEBUG if level <= logging.DEBUG else _BACKUP_COUNT_DEFAULT


def logs_dir(settings: Settings) -> Path:
    """Return the ``<data_dir>/logs`` directory (not created here)."""
    return Path(settings.data_dir).expanduser() / "logs"


def current_log_path(settings: Settings) -> Path:
    """Return the path to the *current* (not-yet-rotated) log file.

    Same file :func:`configure_logging`'s :class:`~logging.handlers.
    RotatingFileHandler` writes to -- COL-131's tail-read endpoint
    (:mod:`collapsarr.system.logs`) reads this path fresh per request rather
    than holding a handle, so it naturally tolerates a rotation happening
    mid-request (the handler renames this path to a numbered backup and
    reopens a fresh file at it).
    """
    return logs_dir(settings) / LOG_FILENAME


def configure_logging(settings: Settings) -> None:
    """Attach a stdout + rotating-file handler pair to the ``collapsarr`` logger.

    Idempotent: clears any handlers/filters already on the logger first, so
    repeated calls (once per ``create_app()``) never accumulate duplicate
    handlers or leak output across a previous call's ``data_dir``/log file.
    """
    logger = logging.getLogger(LOGGER_NAME)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for existing_filter in list(logger.filters):
        logger.removeFilter(existing_filter)

    level = _resolved_level(settings)
    global _env_level
    _env_level = level
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT)
    redaction_filter = _RedactionFilter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redaction_filter)
    logger.addHandler(stream_handler)

    target_dir = logs_dir(settings)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        current_log_path(settings),
        maxBytes=_MAX_BYTES,
        backupCount=_backup_count_for(level),
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction_filter)
    logger.addHandler(file_handler)


def _clear_directory(directory: Path) -> None:
    """Delete every regular file directly inside ``directory`` and recreate it.

    Shared by both branches of :func:`clear_logs` so the delete-then-mkdir
    step isn't duplicated between the "handler attached" and "no handler"
    paths.
    """
    if directory.is_dir():
        for entry in directory.iterdir():
            if entry.is_file():
                entry.unlink()
    directory.mkdir(parents=True, exist_ok=True)


def clear_logs(settings: Settings) -> None:
    """Delete every file under ``logs_dir`` and recreate an empty current file (COL-132).

    Delete-and-recreate, not truncate-in-place, so a truncate racing a handler
    mid-write can never corrupt a record. The subtlety is keeping the live
    :class:`~logging.handlers.RotatingFileHandler` already attached to the
    ``collapsarr`` logger (by a prior :func:`configure_logging` call) working
    *immediately* afterward: a naive unlink-then-mkdir would leave the handler
    holding an open file descriptor into the now-deleted, directory-invisible
    inode -- every subsequent write would silently vanish into it rather than
    the recreated file, until the process restarts or the next size-based
    rollover happens to call :meth:`~logging.handlers.RotatingFileHandler.
    doRollover` and reopen.

    Instead: acquire the handler's own lock first (a :class:`threading.RLock`,
    so its own internal ``close()``/``emit()`` locking nests safely) so the
    whole clear -- close, delete, recreate, reopen -- is atomic against a
    concurrent log write from another thread; ``close()`` flushes and closes
    the stream (without deleting the file itself; it also sets the handler's
    internal ``_closed`` flag, but that's harmless here -- ``FileHandler.emit``
    only ever consults it when ``self.stream is None``, and the stream is
    reassigned below before this function returns, so ``emit`` never observes
    ``stream is None``); every file in ``logs_dir`` is then deleted and the
    directory recreated; and the handler's stream is eagerly reassigned via
    :meth:`~logging.FileHandler._open` -- the same private-method technique
    :meth:`~logging.handlers.RotatingFileHandler.doRollover` itself uses after
    a rename -- so the current file exists, empty, before this returns, and
    the very next record this process emits lands in it. If no
    ``RotatingFileHandler`` is attached at all (e.g. :func:`configure_logging`
    was never called), the files are still deleted and an empty current file
    is created directly -- unguarded by any lock, since there is no handler
    whose concurrent writes it would need to serialize against.
    """
    directory = logs_dir(settings)
    logger = logging.getLogger(LOGGER_NAME)
    file_handler = next(
        (
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ),
        None,
    )

    if file_handler is None:
        _clear_directory(directory)
        current_log_path(settings).touch()
        return

    file_handler.acquire()
    try:
        file_handler.close()
        _clear_directory(directory)
        file_handler.stream = file_handler._open()  # noqa: SLF001 -- mirrors doRollover's own technique
    finally:
        file_handler.release()


def apply_log_level(level_name: str | None) -> None:
    """Apply ``level_name`` to the ``collapsarr`` logger live (COL-130).

    Updates the logger's effective level and, if a
    :class:`~logging.handlers.RotatingFileHandler` is already attached (added
    by a prior :func:`configure_logging` call), its ``backupCount`` (51 at
    DEBUG, 6 otherwise -- see module docstring) to match. ``None`` -- the
    ``GlobalSettings.log_level`` Settings -> General dropdown's unset state --
    falls back to :data:`_env_level`, the most recent
    ``COLLAPSARR_LOG_LEVEL``-resolved level :func:`configure_logging` recorded
    at boot, so clearing a persisted override reverts live too, not only on
    the next restart.

    Deliberately lighter than :func:`configure_logging`: it only ever adjusts
    the *existing* logger/handlers in place, never rebuilds them, so it needs
    no :class:`~collapsarr.config.Settings` (no ``data_dir``/log-file-path
    knowledge is needed for a level-only change) and is safe to call from a
    request handler with nothing more than the level name just persisted.
    """
    logger = logging.getLogger(LOGGER_NAME)
    level = _level_from_name(level_name) if level_name is not None else _env_level
    logger.setLevel(level)
    backup_count = _backup_count_for(level)
    for handler in logger.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            handler.backupCount = backup_count
