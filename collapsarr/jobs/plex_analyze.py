"""Post-job Plex Analyze hook (COL-211).

:mod:`collapsarr.jobs.queue` (COL-20) captures the outcome of every job run
onto its own in-memory :class:`~collapsarr.jobs.queue.Job`. This module is
the bridge from a job's success to Plex's own per-item "Analyze" scan
(:func:`~collapsarr.plex.client.analyze_item`, COL-209) -- the operation that
reliably forces Plex to re-read a file's stream-level metadata (its audio
tracks and their dispositions), as distinct from a broader library
Refresh/Scan. A successful downmix job rewrites the file's audio streams and
a successful Default Audio Track fix changes a stream's disposition; either
way, Plex's own cached metadata is now stale until something tells it to
re-read the file -- this hook is that something.

Resolving *which* Plex item a job's file corresponds to is COL-210's
:func:`~collapsarr.plex.library_sync.resolve_rating_key` (mapping table
first, then a single live-fallback query, then a silent give-up). This
module only adds the last step on top of that: given a resolved
``ratingKey``, call :func:`~collapsarr.plex.client.analyze_item`.

Mirrors :mod:`collapsarr.jobs.failure_notify`'s bridge pattern: a plain
function taking a SQLAlchemy :class:`~sqlalchemy.orm.Session` and a
:class:`~collapsarr.jobs.queue.Job` (:func:`trigger_plex_analyze`), plus a
``session_factory``-bound constructor (:func:`make_plex_analyzer`) matching
:class:`~collapsarr.jobs.queue.JobQueue`'s ``plex_analyzer`` hook signature.

Deliberately never raises, for the same reason
:mod:`collapsarr.jobs.failure_notify` never does: a Plex-side problem --
unreachable server, an unresolved ratingKey, an error response, even a DB
hiccup reading the Plex connection row -- must never be able to fail the
downmix/default-audio job it is reporting on. :func:`trigger_plex_analyze`
wraps all of that in a single ``try``/``except``, belt-and-braces alongside
:class:`~collapsarr.jobs.queue.JobQueue`'s own defensive wrapping of the hook
call (:meth:`~collapsarr.jobs.queue.JobQueue._trigger_plex_analyze`) -- the
same double-layered guarantee :mod:`collapsarr.jobs.failure_notify` gives
:meth:`~collapsarr.jobs.queue.JobQueue._notify_failure`.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.plex import analyze_item, get_plex_connection, resolve_rating_key

from .queue import Job, JobStatus, PlexAnalyzer

logger = logging.getLogger(__name__)


def trigger_plex_analyze(
    session: Session,
    job: Job,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Trigger a Plex Analyze call for ``job``'s file, if Plex is configured and it resolves.

    A no-op -- no Plex call, no error surfaced -- for any of:

    - ``job`` didn't reach :attr:`~collapsarr.jobs.queue.JobStatus.SUCCEEDED`;
    - the Plex connection has no ``base_url`` configured (see
      :func:`~collapsarr.plex.service.get_plex_connection`) -- checked
      *before* any resolution or network call is attempted, so an
      unconfigured Plex genuinely does nothing, not even a mapping-table
      lookup; or
    - the file's Plex ``ratingKey`` can't be resolved (see
      :func:`~collapsarr.plex.library_sync.resolve_rating_key`, which already
      gives up silently on its own miss/error paths).

    Otherwise issues one :func:`~collapsarr.plex.client.analyze_item` call
    for the resolved ``ratingKey`` -- a failed Analyze call (non-2xx, network
    error) is logged and swallowed, same as an unresolved ratingKey.
    ``transport`` is forwarded to both the resolution's live-fallback query
    and the Analyze call -- tests inject an ``httpx.MockTransport``;
    production leaves it ``None`` for a real network call.

    Never raises: any exception -- reading the Plex connection, resolving the
    ratingKey, or issuing the Analyze call -- is caught and logged rather
    than propagated, so a Plex problem can never fail the job it is
    reporting on.
    """
    try:
        if job.status is not JobStatus.SUCCEEDED:
            return

        connection = get_plex_connection(session)
        if not connection.base_url:
            return

        rating_key = resolve_rating_key(
            session,
            job.file_path,
            base_url=connection.base_url,
            token=connection.token,
            transport=transport,
        )
        if rating_key is None:
            return

        result = analyze_item(
            connection.base_url, connection.token, rating_key, transport=transport
        )
        if not result.ok:
            logger.warning(
                "Plex Analyze call failed for job %s (ratingKey=%s): %s",
                job.id,
                rating_key,
                result.error,
            )
    except Exception:  # noqa: BLE001 - a Plex problem must never fail the job
        logger.exception("Failed to trigger Plex Analyze for job %s", job.id)


def make_plex_analyzer(
    session_factory: sessionmaker[Session],
    *,
    transport: httpx.BaseTransport | None = None,
) -> PlexAnalyzer:
    """Build a :class:`~collapsarr.jobs.queue.JobQueue`-compatible ``plex_analyzer``.

    The returned callable opens a **fresh** :class:`~sqlalchemy.orm.Session`
    from ``session_factory`` on every call and closes it again before
    returning -- the same convention :func:`collapsarr.jobs.failure_notify.
    make_failure_notifier`/:func:`collapsarr.jobs.tracked_media.
    make_tracked_media_recorder` use, and for the same reason:
    :class:`~collapsarr.jobs.queue.JobQueue` may invoke this from any of up
    to ``max_concurrency`` worker threads, and SQLAlchemy sessions aren't
    safe to share across threads.

    Typical use::

        session_factory = create_session_factory(engine)
        queue = JobQueue.from_settings(
            plex_analyzer=make_plex_analyzer(session_factory)
        )
    """

    def analyzer(job: Job) -> None:
        with session_factory() as session:
            trigger_plex_analyze(session, job, transport=transport)

    return analyzer
