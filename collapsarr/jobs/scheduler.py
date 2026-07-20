"""Wire the webhook and a periodic library scan into the job queue (COL-22).

Two triggers feed the same de-duplicating enqueue path:

- **Real-time (webhook):** :meth:`JobScheduler.on_file_ready` is the "file
  ready" hook the arr webhook receiver (COL-14) calls once per imported/upgraded
  file. It replaces the log-only stub
  (:func:`collapsarr.arr.webhooks.default_on_file_ready_hook`); the app wires it
  in via ``create_app(enable_scheduler=True)`` (see :mod:`collapsarr.main`).
- **Periodic (scan):** a background thread runs a full-library scan every
  ``settings.scan_interval_hours`` (:meth:`scan_once`), pulling each configured
  instance's monitored file list (COL-12) and enqueuing every file that has a
  qualifying missing downmix target (COL-16).

Both funnel through :meth:`enqueue_file`, which probes the file
(:func:`~collapsarr.downmix.probe.probe_audio_streams`, COL-15), asks
:func:`~collapsarr.downmix.targets.detect_qualifying_targets` whether any target
actually qualifies, and enqueues a real :class:`~collapsarr.jobs.queue.Job` only
when one does -- a file with nothing to do is never enqueued.

De-duplication
--------------
Overlapping triggers (a webhook firing while a scan is mid-flight, or two scans
straddling a slow job) must not enqueue the same file twice. A file is
considered a duplicate -- and skipped -- when either:

- **already queued:** any job for that file path is currently ``PENDING`` or
  ``RUNNING`` in the in-memory queue (:meth:`~collapsarr.jobs.queue.JobQueue.list_jobs`);
  or
- **recently processed:** a persisted job-history row (COL-21) for that file
  path reached a terminal state (``SUCCEEDED``/``FAILED``) within the
  de-duplication window.

The window is the scan interval itself (``settings.scan_interval_hours``). The
reasoning: a successful downmix rewrites the file, so the next scan's re-probe
would already report "nothing to do" -- but a *failed* run leaves the file
unchanged and would otherwise be re-enqueued by every subsequent trigger. Tying
the window to the scan interval means a file is attempted at most once per scan
cycle, which both stops a webhook + scheduled scan from double-enqueuing within
a cycle and prevents a persistently-failing file from being retried faster than
once per cycle, while still allowing a periodic retry after the window elapses.
Checking persisted history (not just the in-memory queue) also covers files
processed in a *previous* process run: after a restart the in-memory queue is
empty, but a file downmixed minutes before the restart is still correctly
skipped.

The dedup check plus the enqueue are performed under a lock so the webhook
thread and the scan thread can't both pass the "not a duplicate" check for the
same file and each enqueue it.

Threads, not asyncio: this matches :mod:`collapsarr.jobs.queue`'s rationale --
the pipeline shells out to blocking ``ffprobe``/``ffmpeg`` subprocesses -- and
avoids pulling in an external scheduler dependency (there is none in
``pyproject.toml``). The loop is a plain sleep/wake ``threading`` loop: it wakes
either on the scan-interval timeout (run a full scan, then drain) or early when
a webhook enqueues work (:attr:`_wake`) so a freshly-enqueued job is drained
promptly rather than waiting out the whole interval.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.arr.files import fetch_monitored_files
from collapsarr.arr.models import resolve_path
from collapsarr.arr.service import list_instances, list_path_mappings
from collapsarr.arr.webhooks import ResolvedWebhookFile
from collapsarr.config import Settings
from collapsarr.downmix.probe import AudioStreamInfo, FfprobeError, probe_audio_streams
from collapsarr.downmix.targets import DownmixSettings, detect_qualifying_targets
from collapsarr.jobs.history import list_job_history
from collapsarr.jobs.queue import Job, JobQueue, JobStatus

logger = logging.getLogger(__name__)

#: Signature of the probe seam: turn a file path into its audio streams. Matches
#: :func:`~collapsarr.downmix.probe.probe_audio_streams` (called positionally),
#: and is injectable so tests need neither ``ffprobe`` nor real media files.
ProbeFn = Callable[[Path], Sequence[AudioStreamInfo]]

#: A job is "in flight" -- and so a duplicate -- when in either of these states.
_ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)
#: A job counts as "recently processed" only once it has reached one of these.
_TERMINAL_STATUSES = (JobStatus.SUCCEEDED, JobStatus.FAILED)

_STOP_JOIN_TIMEOUT = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobScheduler:
    """Enqueue downmix jobs from webhooks and a periodic scan, de-duplicating both.

    ``queue`` is the shared :class:`~collapsarr.jobs.queue.JobQueue` both
    triggers enqueue onto (and that the background loop drains). ``session_factory``
    opens sessions for reading configured instances, path mappings, and job
    history. ``settings`` supplies ``scan_interval_hours`` (both the loop period
    and the dedup window).

    ``downmix_settings`` is the target/language configuration every enqueued job
    is created with; it defaults to :class:`~collapsarr.downmix.targets.DownmixSettings`'s
    own defaults (Stereo only). There is no persisted, per-instance Settings
    model yet -- a single process-wide default mirrors how the rest of the
    downmix engine already takes a ``DownmixSettings`` argument, and a real
    settings store can be threaded through here later.

    ``probe`` and ``now`` are injectable seams for testing (a stub probe and a
    controllable clock); both default to the real implementations.
    """

    def __init__(
        self,
        queue: JobQueue,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        downmix_settings: DownmixSettings | None = None,
        probe: ProbeFn = probe_audio_streams,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._queue = queue
        self._session_factory = session_factory
        self._settings = settings
        self._downmix_settings = downmix_settings or DownmixSettings()
        self._probe = probe
        self._now = now
        self._interval_seconds = settings.scan_interval_hours * 3600.0
        self._dedup_window = timedelta(hours=settings.scan_interval_hours)
        self._enqueue_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- Enqueue path (shared by webhook + scan) ----------------------------

    def on_file_ready(self, file: ResolvedWebhookFile) -> None:
        """Webhook "file ready" hook: enqueue a real downmix job for the file.

        The path on ``file`` has already been translated through the instance's
        path mappings by :func:`~collapsarr.arr.webhooks.resolve_webhook_file`,
        so it is a host-local path ready to probe. Enqueuing a job (rather than
        running the pipeline inline) keeps the webhook response fast; the
        background loop, woken here, drains it promptly.
        """
        job = self.enqueue_file(file.file_path)
        if job is None:
            logger.info(
                "webhook: no job enqueued for %s (duplicate or nothing to do)",
                file.file_path,
            )
            return
        logger.info("webhook: enqueued job %s for %s", job.id, file.file_path)
        self._wake.set()

    def enqueue_file(self, file_path: str | Path, *, session: Session | None = None) -> Job | None:
        """Enqueue a downmix job for ``file_path`` unless it should be skipped.

        Returns the created :class:`~collapsarr.jobs.queue.Job`, or ``None`` when
        the file is a duplicate (already queued / recently processed), has no
        qualifying downmix target, or cannot be probed. ``session`` (when given)
        is reused for the history-based dedup lookup; otherwise a short-lived one
        is opened.

        The cheap dedup check runs first so an already-handled file isn't probed
        needlessly. It is re-checked under :attr:`_enqueue_lock` immediately
        before enqueuing so two concurrent triggers can't both enqueue the same
        file.
        """
        path = Path(file_path)

        if self._is_duplicate(path, session):
            return None

        try:
            streams = self._probe(path)
        except FfprobeError as exc:
            logger.warning("skipping %s: could not probe audio streams: %s", path, exc)
            return None

        if not detect_qualifying_targets(streams, self._downmix_settings):
            return None

        with self._enqueue_lock:
            if self._is_duplicate(path, session):
                return None
            return self._queue.enqueue(path, self._downmix_settings)

    def _is_duplicate(self, path: Path, session: Session | None) -> bool:
        """Whether ``path`` is already queued/running or was recently processed."""
        if self._is_active(path):
            return True
        if session is not None:
            return self._is_recently_processed(path, session)
        with self._session_factory() as owned_session:
            return self._is_recently_processed(path, owned_session)

    def _is_active(self, path: Path) -> bool:
        """Whether a job for ``path`` is currently ``PENDING`` or ``RUNNING``."""
        return any(
            job.file_path == path and job.status in _ACTIVE_STATUSES
            for job in self._queue.list_jobs()
        )

    def _is_recently_processed(self, path: Path, session: Session) -> bool:
        """Whether a terminal history row for ``path`` falls inside the dedup window."""
        cutoff = self._now() - self._dedup_window
        for row in list_job_history(session, file_path=str(path)):
            if row.status not in _TERMINAL_STATUSES or row.ended_at is None:
                continue
            ended = row.ended_at
            if ended.tzinfo is None:  # SQLite round-trips datetimes as naive UTC.
                ended = ended.replace(tzinfo=UTC)
            if ended >= cutoff:
                return True
        return False

    # -- Periodic full-library scan -----------------------------------------

    def scan_once(self) -> list[Job]:
        """Scan every configured instance's monitored files and enqueue qualifying ones.

        Returns the jobs enqueued this pass (skipped/no-op files excluded). A
        fetch failure for one instance is logged and skipped rather than
        aborting the whole scan, so one unreachable Sonarr/Radarr doesn't stop
        the others from being scanned.
        """
        enqueued: list[Job] = []
        with self._session_factory() as session:
            instances = list_instances(session)
            for instance in instances:
                try:
                    files = fetch_monitored_files(instance)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "scan: failed to fetch files from instance %r (id=%s): %s",
                        instance.name,
                        instance.id,
                        exc,
                    )
                    continue
                mappings = list_path_mappings(session, instance.id)
                for monitored in files:
                    local_path = resolve_path(monitored.file_path, mappings)
                    job = self.enqueue_file(local_path, session=session)
                    if job is not None:
                        enqueued.append(job)
        logger.info(
            "scan complete: enqueued %d job(s) across %d instance(s)",
            len(enqueued),
            len(instances),
        )
        return enqueued

    # -- Background loop lifecycle ------------------------------------------

    def start(self) -> None:
        """Start the background scan/drain loop in a daemon thread.

        Runs an initial scan immediately, then repeats every
        ``scan_interval_hours``. Idempotency is the caller's responsibility --
        calling this twice raises.
        """
        if self._thread is not None:
            raise RuntimeError("JobScheduler is already started")
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="collapsarr-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float | None = _STOP_JOIN_TIMEOUT) -> None:
        """Signal the loop to stop and join its thread (a no-op if not started)."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        """Sleep/wake loop: full scan on the interval, prompt drain when woken."""
        next_scan = time.monotonic()  # scan immediately on the first iteration
        while not self._stop.is_set():
            if time.monotonic() >= next_scan:
                try:
                    self.scan_once()
                except Exception:  # noqa: BLE001 - one bad scan must not kill the loop
                    logger.exception("scheduled library scan failed")
                next_scan = time.monotonic() + self._interval_seconds
            self._drain()
            if self._stop.is_set():
                break
            self._wake.wait(timeout=max(0.0, next_scan - time.monotonic()))
            self._wake.clear()

    def _drain(self) -> None:
        """Run every pending job to completion, capturing (not raising) failures."""
        try:
            self._queue.run_pending()
        except Exception:  # noqa: BLE001 - a drain failure must not kill the loop
            logger.exception("draining the job queue failed")
