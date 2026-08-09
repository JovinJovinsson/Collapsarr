"""GET /api/system/logs -- tail-read of the current log file (COL-131).

A thin, read-only ``/api/system`` view -- same shape as
:mod:`collapsarr.system.tasks` (COL-122) / :mod:`collapsarr.system.info`
(COL-123) -- over the *current* rotating log file COL-128's
:func:`~collapsarr.logging_setup.configure_logging` writes to
(:func:`~collapsarr.logging_setup.current_log_path`). Only the current file is
read here -- rotated backups (``collapsarr.log.1``, ``.2``, ...) are out of
scope for this ticket.

Endpoint:

* ``GET /api/system/logs`` -- the most recent ``~200`` lines of the current
  log file by default, oldest first / newest last (matching how a terminal
  ``tail`` reads), with an ``offset`` query parameter to page further back and
  a ``level`` query parameter applying *minimum-severity* filtering (e.g.
  ``level=WARNING`` returns WARNING and ERROR lines), filtered while scanning
  the file server-side -- never by post-filtering an already-fetched window.

Rotation race (ticket AC): :class:`~logging.handlers.RotatingFileHandler`
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
from pathlib import Path
from typing import Literal, NamedTuple

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ..config import Settings
from ..logging_setup import current_log_path

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


__all__ = ["DEFAULT_LIMIT", "LogEntryRead", "LogLevelFilter", "LogsRead", "router"]
