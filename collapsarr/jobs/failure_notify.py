"""Downmix-failure -> notifier dispatch bridge (COL-37).

:mod:`collapsarr.jobs.queue` (COL-20) captures the outcome of every job run
onto its own in-memory :class:`~collapsarr.jobs.queue.Job`. This module is
the bridge from that outcome to :mod:`collapsarr.notify.dispatch`'s fan-out
sender (COL-35): when a job's terminal status is ``FAILED``,
:func:`notify_job_failure` builds a :class:`~collapsarr.notify.dispatch.
NotificationEvent` carrying the file, target/language, and error detail, and
dispatches it to every notifier enabled on the persisted
:class:`~collapsarr.notify.models.NotifierConfig` row.

Mirrors :mod:`collapsarr.jobs.history`'s bridge pattern
(:func:`~collapsarr.jobs.history.make_history_recorder`): plain functions
taking a SQLAlchemy :class:`~sqlalchemy.orm.Session`, plus a
``session_factory``-bound constructor
(:func:`make_failure_notifier`) matching :class:`~collapsarr.jobs.queue.
JobQueue`'s ``failure_notifier`` hook signature.

Deliberately never raises: :func:`~collapsarr.notify.dispatch.
dispatch_notification` itself already never raises (per-notifier network
failures are isolated there), but *this* module also owns opening a DB
session and reading the notifier config, either of which could fail for
reasons unrelated to the notifiers themselves (e.g. the database being
briefly unavailable). :func:`notify_job_failure` wraps all of that in a
single ``try``/``except`` so a notification problem can never propagate up
and fail the downmix job it is reporting on -- the acceptance criterion
:class:`~collapsarr.jobs.queue.JobQueue._notify_failure` is *also* defensive
about, belt-and-braces, since this is the one guarantee the whole feature
must never break.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.notify import NotificationEvent, dispatch_notification, get_notifier_config

from .queue import FailureNotifier, Job

logger = logging.getLogger(__name__)

EVENT_TYPE = "downmix_failure"


def _error_detail(job: Job) -> str:
    """The failure's human-readable text -- from the runner exception, if any,
    else the pipeline result's failure detail."""
    if job.error is not None:
        return str(job.error)
    if job.result is not None and not job.result.success:
        return job.result.detail
    return "Unknown error"  # pragma: no cover - defensive; _run_job never leaves both unset


def _target(job: Job) -> str | None:
    """Comma-joined enabled downmix targets the job was configured with."""
    if not job.settings.enabled_targets:
        return None
    return ",".join(sorted(target.value for target in job.settings.enabled_targets))


def _language(job: Job) -> str | None:
    """Comma-joined language allow-list, or ``None`` when unrestricted."""
    if job.settings.language_allow_list is None:
        return None
    return ",".join(sorted(job.settings.language_allow_list))


def _build_event(job: Job) -> NotificationEvent:
    """Build the :class:`NotificationEvent` describing ``job``'s failure."""
    details: dict[str, str] = {"file": str(job.file_path), "error": _error_detail(job)}
    target = _target(job)
    if target is not None:
        details["target"] = target
    language = _language(job)
    if language is not None:
        details["language"] = language

    return NotificationEvent(
        event_type=EVENT_TYPE,
        title="Downmix failed",
        message=f"Downmix failed for {job.file_path}",
        details=details,
    )


def notify_job_failure(
    session: Session,
    job: Job,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Dispatch a downmix-failure notification for ``job``.

    Reads the singleton :class:`~collapsarr.notify.models.NotifierConfig` row
    via ``session`` (creating it with defaults, i.e. both notifiers
    disabled, on first call -- see :func:`~collapsarr.notify.service.
    get_notifier_config`) and fans the event out to every notifier enabled
    on it. ``transport`` is forwarded to :func:`~collapsarr.notify.dispatch.
    dispatch_notification` -- tests inject an ``httpx.MockTransport``;
    production leaves it ``None`` for a real network call.

    Never raises: any exception -- from reading the config, building the
    event, or dispatching it -- is caught and logged rather than propagated,
    so a notification problem can never fail the underlying job.
    """
    try:
        config = get_notifier_config(session)
        event = _build_event(job)
        dispatch_notification(config, event, transport=transport)
    except Exception:  # noqa: BLE001 - a notification problem must never fail the job
        logger.exception("Failed to dispatch downmix-failure notification for job %s", job.id)


def make_failure_notifier(
    session_factory: sessionmaker[Session],
    *,
    transport: httpx.BaseTransport | None = None,
) -> FailureNotifier:
    """Build a :class:`~collapsarr.jobs.queue.JobQueue`-compatible ``failure_notifier``.

    The returned callable opens a **fresh** :class:`~sqlalchemy.orm.Session`
    from ``session_factory`` on every call and closes it again before
    returning -- never reusing or sharing one session across calls, the same
    convention :func:`collapsarr.jobs.history.make_history_recorder` uses and
    for the same reason: :class:`~collapsarr.jobs.queue.JobQueue` may invoke
    this from any of up to ``max_concurrency`` worker threads, and SQLAlchemy
    sessions aren't safe to share across threads.

    Typical use::

        session_factory = create_session_factory(engine)
        queue = JobQueue.from_settings(
            failure_notifier=make_failure_notifier(session_factory)
        )
    """

    def notifier(job: Job) -> None:
        with session_factory() as session:
            notify_job_failure(session, job, transport=transport)

    return notifier
