"""Hard-kill a downmix job's in-flight subprocess on manual cancel (COL-192).

The Downmix Engine shells out to ``ffprobe``/``ffmpeg`` via blocking
:mod:`subprocess` calls (:mod:`collapsarr.downmix.probe`,
:mod:`collapsarr.downmix.remux`, reached through
:mod:`collapsarr.downmix.pipeline`'s injectable ``runner`` seam). Before
COL-192 there was no handle on the running process, so a job could only be
cancelled while still ``PENDING`` -- once a worker claimed it and ffmpeg was
running, the cancel endpoint reported "too late" (ADR 0007's "no interruption
of an in-flight ffmpeg"). This module supplies the missing handle so a
``RUNNING`` job can be terminated immediately on an explicit user cancel.

Two pieces, deliberately small and free of any job-queue import (so both
:mod:`collapsarr.jobs.queue` -- which creates and cancels the handle -- and
:mod:`collapsarr.downmix.pipeline` -- which feeds it the live process -- can
depend on this module without a cycle):

* :class:`CancellationHandle` -- a thread-safe rendezvous between the worker
  thread running a job's subprocesses and a concurrent
  :meth:`~collapsarr.jobs.queue.JobQueue.cancel_running` call. The worker
  :meth:`~CancellationHandle.attach`\\ es each subprocess as it spawns and
  :meth:`~CancellationHandle.detach`\\ es it when it finishes; a cancel flips
  the handle and kills whatever is currently attached (and, if the cancel
  lands between two stages, kills the *next* subprocess the moment it attaches,
  via the ``cancelled`` latch).
* :func:`make_cancellable_runner` -- a drop-in for the pipeline's ``runner``
  seam that spawns each subprocess in its **own process group**
  (``start_new_session=True``) so a kill takes down ffmpeg *and any children*
  it spawned, and registers it with a :class:`CancellationHandle` for the
  duration of the call.

**No new terminal status.** Killing the subprocess makes the in-flight
``subprocess`` call return a non-zero (signal) exit code, so the existing
pipeline reports an ordinary failure and the job transitions to
:attr:`~collapsarr.jobs.queue.JobStatus.FAILED` through the exact same path a
real ffmpeg failure already takes -- no ``CANCELLED`` status, and so no
job-history schema migration, is introduced (see
``docs/adr/0007-*.md``). A cancelled job is therefore indistinguishable from a
failed one in history, which the ticket explicitly permits.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from typing import Protocol

#: Signature the pipeline's ``runner`` seam expects, matching the private
#: ``_Runner`` alias each downmix module already declares:
#: ``(command, timeout) -> subprocess.CompletedProcess``.
Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


class KillableProcess(Protocol):
    """The slice of :class:`subprocess.Popen` the cancellation machinery needs.

    Kept to just ``pid``/``poll``/``kill`` -- the three members
    :func:`_terminate_process_tree` actually touches -- so the handle depends
    on a structural interface rather than the whole ``Popen`` surface (a real
    ``Popen`` satisfies it, and so can a lightweight test double).
    """

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


def _terminate_process_tree(process: KillableProcess) -> None:
    """Kill ``process`` and every child in its process group, immediately.

    A no-op if the process has already exited. Prefers ``killpg`` against the
    process group id (only reliable when the process was spawned with its own
    session/group -- see :func:`make_cancellable_runner`, which does) so a
    child ffmpeg spawned is taken down too; falls back to killing just the
    process itself where a process group can't be resolved (already reaped, or
    a platform without ``killpg``). Every failure mode is swallowed: the only
    goal is "make sure it's dead," and a process that vanished on its own
    between the poll and the signal has already achieved that.
    """
    if process.poll() is not None:
        return  # already exited -- nothing to kill

    pgid: int | None = None
    if hasattr(os, "getpgid"):
        try:
            pgid = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            pgid = None

    if pgid is not None and hasattr(os, "killpg"):
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, OSError):
            pass  # fall through to a plain process kill

    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass


class CancellationHandle:
    """Per-job hard-kill rendezvous between a worker and a concurrent cancel (COL-192).

    Created by :class:`~collapsarr.jobs.queue.JobQueue` for a job the instant
    it transitions to ``RUNNING`` and stored on the
    :class:`~collapsarr.jobs.queue.Job`, so a later
    :meth:`~collapsarr.jobs.queue.JobQueue.cancel_running` call can reach the
    live subprocess. The worker thread running the job's pipeline
    :meth:`attach`\\ es each subprocess as it spawns and :meth:`detach`\\ es it
    on completion (via :func:`make_cancellable_runner`); :meth:`cancel` latches
    the handle cancelled and kills whatever is attached right now.

    The ``cancelled`` latch closes the gap between two subprocesses of one job
    (e.g. a cancel arriving after ffprobe finished but before ffmpeg starts):
    :meth:`attach` kills its process on the spot if the handle was already
    cancelled, so the job can never spawn a fresh subprocess after a cancel.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._process: KillableProcess | None = None

    @property
    def cancelled(self) -> bool:
        """Whether :meth:`cancel` has been called on this handle."""
        with self._lock:
            return self._cancelled

    def attach(self, process: KillableProcess) -> None:
        """Track ``process`` as this job's live subprocess.

        If the handle was already cancelled before this subprocess spawned,
        the process is killed immediately (and not tracked) so a job cannot
        start new work after its cancel landed.
        """
        with self._lock:
            already_cancelled = self._cancelled
            if not already_cancelled:
                self._process = process
        if already_cancelled:
            _terminate_process_tree(process)

    def detach(self, process: KillableProcess) -> None:
        """Stop tracking ``process`` (it has finished, or is about to be reaped)."""
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> None:
        """Latch the handle cancelled and hard-kill the attached subprocess, if any.

        Idempotent and safe to call from any thread. Any subprocess attached
        *after* this returns is killed on attach (see :meth:`attach`), so the
        job's whole remaining subprocess chain is stopped, not just the one
        running at this instant.
        """
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None:
            _terminate_process_tree(process)


def make_cancellable_runner(handle: CancellationHandle) -> Runner:
    """Build a pipeline ``runner`` that registers each subprocess with ``handle`` (COL-192).

    A drop-in for the ``runner`` seam
    :func:`~collapsarr.downmix.pipeline.run_downmix_pipeline` (and the probe/
    remux/apply stages it threads it through) already accept -- same
    ``(command, timeout) -> subprocess.CompletedProcess`` contract as the
    default :func:`subprocess.run` wrapper -- but it spawns via
    :class:`subprocess.Popen` with ``start_new_session=True`` so the child gets
    its own process group, then :meth:`~CancellationHandle.attach`\\ es it to
    ``handle`` for the lifetime of the call. A concurrent
    :meth:`CancellationHandle.cancel` therefore kills this subprocess (and any
    children) at once; the interrupted ``communicate`` returns a non-zero
    signal exit code, which the probe/remux layer reports as an ordinary
    failure.

    Preserves the default runner's contract exactly otherwise: captures
    stdout/stderr as text, and re-raises :class:`subprocess.TimeoutExpired`
    (after killing and reaping the process) so
    :func:`~collapsarr.downmix.remux.run_remux` /
    :func:`~collapsarr.downmix.probe.probe_audio_streams` handle a timeout the
    same way they always have.
    """

    def run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(  # noqa: S603 - command is fixed flags + paths, not shell text
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        handle.attach(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            process.communicate()  # reap the killed child so it can't linger as a zombie
            raise
        finally:
            handle.detach(process)
        return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)

    return run
