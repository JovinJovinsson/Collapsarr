"""Tests for the hard-kill cancellation primitives (COL-192).

Two layers: fast unit tests of :class:`~collapsarr.downmix.cancellation.
CancellationHandle`'s attach/detach/cancel state machine against a fake
process, and one real-subprocess test proving
:func:`~collapsarr.downmix.cancellation.make_cancellable_runner` actually
terminates a live process tree when the handle is cancelled from another
thread.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

import pytest

from collapsarr.downmix.cancellation import CancellationHandle, make_cancellable_runner


class _FakeProcess:
    """A killable stand-in shaped for ``_terminate_process_tree`` (see test_jobs_queue)."""

    def __init__(self) -> None:
        self.pid = 2_000_000_000  # no such process -> os.getpgid raises, forcing kill()
        self.killed = False

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed = True


def test_cancel_kills_the_currently_attached_process() -> None:
    handle = CancellationHandle()
    process = _FakeProcess()
    handle.attach(process)

    handle.cancel()

    assert handle.cancelled is True
    assert process.killed is True


def test_attach_after_cancel_kills_the_process_immediately() -> None:
    """The cancelled latch: a subprocess spawned after a cancel is killed on attach."""
    handle = CancellationHandle()
    handle.cancel()

    process = _FakeProcess()
    handle.attach(process)

    assert process.killed is True  # never given a chance to run


def test_detach_stops_the_handle_from_killing_a_finished_process() -> None:
    handle = CancellationHandle()
    process = _FakeProcess()
    handle.attach(process)
    handle.detach(process)

    handle.cancel()

    assert handle.cancelled is True
    assert process.killed is False  # already detached -- nothing to kill


def test_cancel_is_idempotent_and_safe_with_nothing_attached() -> None:
    handle = CancellationHandle()

    handle.cancel()
    handle.cancel()  # must not raise with no process attached

    assert handle.cancelled is True


@pytest.mark.skipif(
    shutil.which("sleep") is None or not hasattr(os, "killpg"),
    reason="needs a POSIX `sleep` binary and process-group signalling",
)
def test_make_cancellable_runner_terminates_a_live_subprocess_tree() -> None:
    """A concurrent cancel kills the real subprocess: it returns fast with a signal code."""
    handle = CancellationHandle()
    run = make_cancellable_runner(handle)
    outcome: dict[str, subprocess.CompletedProcess[str]] = {}

    def _run() -> None:
        # A 30s sleep stands in for a long ffmpeg remux; the cancel must cut it
        # short well before that.
        outcome["result"] = run(["sleep", "30"], 60.0)

    worker = threading.Thread(target=_run)
    started = time.monotonic()
    worker.start()

    # Give the subprocess a moment to spawn, then cancel from this thread. Even
    # if the cancel raced ahead of attach, the cancelled latch guarantees the
    # process is killed the instant it attaches -- so the sleep never survives.
    time.sleep(0.2)
    handle.cancel()

    worker.join(timeout=10)
    assert not worker.is_alive(), "cancellable runner did not return after the kill"
    assert time.monotonic() - started < 10  # nowhere near the 30s sleep
    # Killed by a signal -> negative return code (SIGKILL == -9).
    assert outcome["result"].returncode < 0
