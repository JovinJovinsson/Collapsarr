"""``/api/system/logs`` -- tail-read, file listing, download, and clear (COL-131/132).

A thin, read-only-plus-housekeeping ``/api/system`` view -- same shape as
:mod:`collapsarr.system.tasks` (COL-122) / :mod:`collapsarr.system.info`
(COL-123) -- over COL-128's rotating log files under
:func:`~collapsarr.logging_setup.logs_dir` (the *current* file at
:func:`~collapsarr.logging_setup.current_log_path`, plus rotated backups
``collapsarr.log.1``, ``.2``, ...).

Endpoints:

* ``GET /api/system/logs`` (COL-131) -- the most recent ``~200`` lines of the
  *current* log file by default, oldest first / newest last (matching how a
  terminal ``tail`` reads), with an ``offset`` query parameter to page further
  back and a ``level`` query parameter applying *minimum-severity* filtering
  (e.g. ``level=WARNING`` returns WARNING and ERROR lines), filtered while
  scanning the file server-side -- never by post-filtering an already-fetched
  window. Only the current file is read here -- rotated backups are out of
  scope for this endpoint (see the listing/download endpoints below for
  those).
* ``GET /api/system/logs/files`` (COL-132) -- every file under
  :func:`~collapsarr.logging_setup.logs_dir` (current + rotated), sorted by
  modification time descending, with name/size/last-written metadata.
* ``GET /api/system/logs/files/{name}/download`` (COL-132) -- streams one log
  file off disk as a plain-text download, mirroring
  ``GET /api/system/backup/{id}/download`` (:mod:`collapsarr.backup.routes`)
  exactly: auth-gated (inherited from the ``/api/system/*`` middleware, no
  extra wiring), streams straight off disk with no separate asset directory,
  ``404`` on an unresolvable ``name`` rather than distinguishing why (see
  :func:`_resolve_log_file_path`).
* ``DELETE /api/system/logs`` (COL-132) -- deletes every file under
  ``logs_dir`` and recreates an empty current file, via
  :func:`~collapsarr.logging_setup.clear_logs` -- delete-and-recreate, not
  truncate-in-place, so it can't corrupt a handler mid-write. See that
  function's docstring for how it keeps the live
  :class:`~logging.handlers.RotatingFileHandler` writing correctly
  immediately afterward.

Rotation race (COL-131 ticket AC): :class:`~logging.handlers.RotatingFileHandler`
closes, renames, and reopens the current file entirely outside any lock this
process could take. This module never holds a long-lived file handle --
:func:`_read_log_lines` opens the file fresh on every call and briefly retries
a missing file (the handler's close-rename-reopen sequence completes in
microseconds), returning an empty read rather than a ``404`` if the file is
still missing after retrying -- indistinguishable, from this endpoint's
perspective, from a fresh/empty log.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import Settings
from ..logging_setup import clear_logs, current_log_path, logs_dir

router = APIRouter(prefix="/api/system", tags=["system"])

#: The four levels selectable as the minimum-severity filter, matching
#: :data:`collapsarr.settings.routes.LogLevelMode` / :data:`collapsarr.
#: settings.models.LOG_LEVELS` (the Settings -> General log-level dropdown) --
#: kept as its own local ``Literal`` (rather than imported) per the precedent
#: :mod:`collapsarr.system.info`'s ``InstallMethod`` sets for a closed,
#: request-facing mode field owned by this module.
LogLevelFilter = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

#: Numeric ranking for every level that can appear in a log line, including
#: ``CRITICAL`` -- not selectable as a filter value (mirrors the dropdown's
#: exclusion), but a line logged at it must still rank *above* ``ERROR`` so an
#: ``level=ERROR`` filter still includes it.
_LEVEL_VALUES: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

#: Matches one log record's header line, as written by
#: :data:`collapsarr.logging_setup._FORMAT`
#: (``"%(asctime)s %(levelname)s %(name)s %(message)s"``) with the default
#: ``asctime`` rendering, e.g. ``"2026-08-09 12:34:56,789 WARNING
#: collapsarr.arr.catalog message text"``. A line that doesn't match (e.g. a
#: continuation line of a multi-line traceback logged via ``exc_info=``) has
#: no level of its own -- see :func:`_parse_lines`.
_LOG_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (?P<level>[A-Z]+) "
)

#: The default (and only, per the ticket AC -- no client-tunable window size
#: was asked for) tail window size.
DEFAULT_LIMIT = 200

_READ_RETRIES = 5
_READ_RETRY_DELAY_SECONDS = 0.02


# --- schemas -----------------------------------------------------------------


class LogEntryRead(BaseModel):
    """One line of the current log file.

    ``line_number`` is 1-based within *this request's* read of the file --
    stable enough to key a table row on for one response, but not a durable
    id across requests (the file keeps growing/rotating). ``level`` is the
    parsed severity of the record this line belongs to (inherited across a
    multi-line record's continuation lines -- see :func:`_parse_lines`), or
    ``None`` for a line that precedes any recognisable record header (should
    not happen against a file this app itself writes, but tolerated rather
    than raising).
    """

    line_number: int
    level: str | None
    text: str


class LogsRead(BaseModel):
    """Response shape for ``GET /api/system/logs``.

    ``entries`` is oldest first / newest last, matching how a terminal
    ``tail`` reads. ``next_offset`` is ``None`` when there is nothing further
    back to page to; otherwise pass it as the next request's ``offset`` to
    fetch the entries immediately before this window (under the same
    ``level`` filter).
    """

    entries: list[LogEntryRead]
    next_offset: int | None


class LogFileRead(BaseModel):
    """One file under ``logs/`` -- the current file or a rotated backup.

    ``name`` is the bare filename (``collapsarr.log``, ``collapsarr.log.1``,
    ...) -- the id the download endpoint's ``{name}`` path segment expects,
    same shape as :class:`~collapsarr.backup.routes.BackupRead`'s ``id`` for
    backups.
    """

    name: str
    size: int
    modified_at: datetime


class LogFileListRead(BaseModel):
    """Response shape for ``GET /api/system/logs/files``."""

    files: list[LogFileRead]


# --- helpers -------------------------------------------------------------


class _ParsedLine(NamedTuple):
    """A parsed log line, internal to this module -- see :class:`LogEntryRead`
    for the API-facing shape."""

    line_number: int
    level: str | None
    text: str


def _read_log_lines(path: Path) -> list[str]:
    """Read every line of ``path``, tolerating a rotation-race missing file.

    Opens the file fresh -- never a cached/held handle -- so a concurrent
    :class:`~logging.handlers.RotatingFileHandler` rollover (close, rename,
    reopen) racing this read is only ever observed as a transient
    ``FileNotFoundError``, briefly retried. Returns ``[]`` if the file is
    still missing after retrying (a genuinely fresh/not-yet-created log, or an
    extraordinarily unlucky multi-rollover race) rather than raising --
    callers treat that the same as an empty log.
    """
    for attempt in range(_READ_RETRIES):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return handle.read().splitlines()
        except FileNotFoundError:
            if attempt < _READ_RETRIES - 1:
                time.sleep(_READ_RETRY_DELAY_SECONDS)
    return []


def _parse_lines(lines: list[str]) -> list[_ParsedLine]:
    """Assign a 1-based line number and severity level to every line.

    A line matching :data:`_LOG_LINE_RE` starts a new record and sets the
    "current" level; a non-matching line (a multi-line traceback's
    continuation) inherits whatever level the most recent matching line
    established, so filtering by minimum severity keeps a record's
    continuation lines together with its header rather than silently
    dropping them.
    """
    current_level: str | None = None
    parsed: list[_ParsedLine] = []
    for index, text in enumerate(lines, start=1):
        match = _LOG_LINE_RE.match(text)
        if match:
            current_level = match.group("level")
        parsed.append(_ParsedLine(line_number=index, level=current_level, text=text))
    return parsed


def _filter_by_level(lines: list[_ParsedLine], minimum: LogLevelFilter | None) -> list[_ParsedLine]:
    """Keep only lines whose record's level is >= ``minimum`` severity.

    Applied while the file is still fully parsed in memory, *before*
    pagination slices out a window -- so ``offset`` counts matching lines
    only, never lines the filter would have excluded (the ticket AC's
    "scanned server-side" requirement). A line with no resolvable level (see
    :func:`_parse_lines`) is dropped once a ``minimum`` is given, since its
    severity relative to ``minimum`` can't be determined.
    """
    if minimum is None:
        return lines
    threshold = _LEVEL_VALUES[minimum]
    return [
        line
        for line in lines
        if line.level is not None and _LEVEL_VALUES.get(line.level, threshold) >= threshold
    ]


def _paginate(
    lines: list[_ParsedLine], *, offset: int, limit: int
) -> tuple[list[_ParsedLine], int | None]:
    """Slice the most recent ``limit`` lines starting ``offset`` back from the tail.

    ``offset=0`` (the default) returns the newest ``limit`` lines. A larger
    ``offset`` pages further back -- ``offset`` counts *already-fetched*
    matching lines from the tail, so passing back a prior response's
    ``next_offset`` continues exactly where it left off. Returns the window
    (oldest first / newest last within it) and the ``next_offset`` to request
    the lines immediately before it, or ``None`` once the start of the file
    has been reached.
    """
    total = len(lines)
    end = total - offset
    if end <= 0:
        return [], None
    start = max(end - limit, 0)
    window = lines[start:end]
    next_offset = offset + len(window) if start > 0 else None
    return window, next_offset


def _file_info(entry: Path) -> LogFileRead:
    stat = entry.stat()
    return LogFileRead(
        name=entry.name,
        size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def _list_log_files(settings: Settings) -> list[LogFileRead]:
    """List every file under ``logs_dir`` (current + rotated), newest first.

    Mirrors :func:`collapsarr.backup.service.list_backups`'s shape. The
    directory may not exist yet on a fresh install that hasn't logged
    anything -- treated the same as empty, not an error.
    """
    directory = logs_dir(settings)
    if not directory.is_dir():
        return []
    files = [_file_info(entry) for entry in directory.iterdir() if entry.is_file()]
    files.sort(key=lambda info: info.modified_at, reverse=True)
    return files


def _resolve_log_file_path(settings: Settings, name: str) -> Path | None:
    """Resolve a :class:`LogFileRead` ``name`` back to its path under ``logs_dir``.

    Mirrors :func:`collapsarr.backup.service.resolve_backup_path`'s shape,
    minus the fixed filename glob (rotated log names vary with
    ``backupCount``, unlike the fixed backup-archive naming). ``None`` (never
    an exception) covers every way ``name`` can fail to name a real file:
    empty, ``.``/``..``, an embedded path separator (rules out traversal such
    as ``../../etc/passwd``), or no such file directly inside ``logs_dir``.
    The download route maps every ``None`` uniformly to a ``404``.
    """
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        return None
    candidate = logs_dir(settings) / name
    if candidate.name != name or not candidate.is_file():
        return None
    return candidate


# --- endpoints ---------------------------------------------------------------


@router.get("/logs", response_model=LogsRead)
def get_logs_endpoint(
    request: Request,
    level: LogLevelFilter | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
) -> LogsRead:
    """Return a tail window of the current log file, newest last.

    ``level`` applies minimum-severity filtering while the file is parsed
    (WARNING returns WARNING and ERROR, matching the ticket AC), before
    ``offset`` pagination is applied -- so paging further back with a
    ``level`` filter set only ever advances through matching lines, never
    skips over them uncounted.
    """
    settings: Settings = request.app.state.settings
    lines = _read_log_lines(current_log_path(settings))
    parsed = _filter_by_level(_parse_lines(lines), level)
    window, next_offset = _paginate(parsed, offset=offset, limit=DEFAULT_LIMIT)
    return LogsRead(
        entries=[LogEntryRead(**line._asdict()) for line in window],
        next_offset=next_offset,
    )


@router.delete("/logs", status_code=204)
def clear_logs_endpoint(request: Request) -> None:
    """Delete every log file and recreate an empty current file (COL-132).

    Delete-and-recreate rather than truncate-in-place, so a truncate racing a
    handler mid-write can't corrupt a record -- see
    :func:`~collapsarr.logging_setup.clear_logs` for how it also keeps the
    live ``RotatingFileHandler`` writing correctly to the recreated file
    immediately afterward.
    """
    settings: Settings = request.app.state.settings
    clear_logs(settings)


@router.get("/logs/files", response_model=LogFileListRead)
def list_log_files_endpoint(request: Request) -> LogFileListRead:
    """List every file under ``logs/`` (current + rotated), newest first."""
    settings: Settings = request.app.state.settings
    return LogFileListRead(files=_list_log_files(settings))


@router.get("/logs/files/{name}/download")
def download_log_file_endpoint(name: str, request: Request) -> FileResponse:
    """Stream one log file off disk as a plain-text download.

    Mirrors ``GET /api/system/backup/{id}/download`` exactly (COL-64): auth
    inherited from the ``/api/system/*`` middleware, streamed straight off
    disk with no separate asset directory. ``name`` resolves via
    :func:`_resolve_log_file_path`, which never raises -- an
    unknown/invalid/path-traversal name maps uniformly to a ``404``.
    """
    settings: Settings = request.app.state.settings
    path = _resolve_log_file_path(settings, name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No log file named {name!r}")
    return FileResponse(path, media_type="text/plain", filename=path.name)


__all__ = [
    "DEFAULT_LIMIT",
    "LogEntryRead",
    "LogFileListRead",
    "LogFileRead",
    "LogLevelFilter",
    "LogsRead",
    "router",
]
