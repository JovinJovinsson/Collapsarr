"""Mechanism-selection gate for the Default Audio Track Job (COL-247).

:mod:`~collapsarr.downmix.default_audio_pipeline` (COL-153/COL-241) fixes a
file's Default Audio Track disposition by remuxing it with ffmpeg.
:mod:`~collapsarr.plex.default_audio_write` (COL-245) fixes the same
disposition with a direct write against a configured Plex Media Server
instead -- no ffprobe, no ffmpeg, no local file mutation at all. Both are
fully built and independently tested; this module is the single place that
decides, *once per Job*, which of the two actually runs.

The gate is deliberately narrow: whether the singleton
:class:`~collapsarr.plex.models.PlexConnection` row is configured
(:attr:`~collapsarr.plex.models.PlexConnection.is_configured` -- both
``base_url`` and ``token`` set), read fresh on every call so a Plex
Connection added or removed between two Jobs is honoured on the very next
one, with no restart required. Configured routes to
:func:`~collapsarr.plex.default_audio_write.apply_default_audio_via_plex`;
not configured routes to :func:`~collapsarr.downmix.default_audio_pipeline.
run_default_audio_pipeline`. The two are mutually exclusive branches of one
``if``, not a fallback chain: a Plex-write failure does not retry through
the remux path, and there is no path where both run for the same file
(COL-247's core acceptance criterion).

:func:`make_default_audio_pipeline_runner` builds a
:data:`~collapsarr.jobs.queue.DefaultAudioPipelineRunner` -- the exact seam
:class:`~collapsarr.jobs.queue.JobQueue`'s ``default_audio_pipeline_runner``
constructor argument already accepts (COL-155) -- so wiring this gate into a
real queue is a one-line change at the one call site that builds one
(:meth:`~collapsarr.jobs.queue.JobQueue.from_settings`); no change to
:meth:`~collapsarr.jobs.queue.JobQueue._run_job`'s dispatch itself, which
already just calls whatever runner it was given.

Adapts :func:`~collapsarr.plex.default_audio_write.apply_default_audio_via_plex`'s
:class:`~collapsarr.plex.default_audio_write.PlexDefaultAudioResult` into a
:class:`~collapsarr.downmix.pipeline.PipelineResult` -- the type
:class:`~collapsarr.jobs.queue.Job.result` is documented to always hold
regardless of which mechanism actually ran (:class:`~collapsarr.jobs.queue.Job`'s
own docstring: "both pipelines return this same type") -- so job history,
the failure notifier, and every other consumer of ``job.result`` need no
changes to understand a Plex-write outcome.
:attr:`~collapsarr.plex.default_audio_write.PlexDefaultAudioOutcome.SUCCESS`/
``NOTHING_TO_DO`` map to their :class:`~collapsarr.downmix.pipeline.PipelineOutcome`
namesakes; every other (failure) outcome maps to
:attr:`~collapsarr.downmix.pipeline.PipelineOutcome.APPLY_FAILED` -- the
closest existing meaning ("the fix could not be applied"). Nothing outside
:mod:`~collapsarr.downmix.pipeline` and :mod:`~collapsarr.downmix.
default_audio_pipeline` reads ``outcome`` at all (only ``success``/``detail``
reach job history/notifications), so this collapsing loses no information
any real caller consumes. ``detail`` and ``success`` are always carried
through unchanged.

A fresh DB session is opened only for the decision itself and, if Plex is
connected, for the (fast, HTTP-only) Plex-write call -- mirroring
:mod:`~collapsarr.jobs.plex_analyze`'s ``session_factory``-bound convention,
and for the same thread-safety reason (a :class:`~collapsarr.jobs.queue.
JobQueue` worker pool may call this from any of up to ``max_concurrency``
threads). It is always closed *before* falling through to the remux
mechanism, which can run for up to an hour (``remux_timeout``) shelling out
to ffmpeg -- this gate must never hold a DB session open for the duration of
a remux.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy.orm import Session, sessionmaker

from collapsarr.downmix.default_audio import DefaultAudioPreference
from collapsarr.downmix.default_audio_pipeline import run_default_audio_pipeline
from collapsarr.downmix.pipeline import PipelineOutcome, PipelineResult
from collapsarr.plex import (
    PlexDefaultAudioOutcome,
    PlexDefaultAudioResult,
    apply_default_audio_via_plex,
    get_plex_connection,
)

from .queue import DefaultAudioPipelineRunner

logger = logging.getLogger(__name__)

#: Signature :func:`~collapsarr.plex.default_audio_write.apply_default_audio_via_plex`
#: matches -- injectable so a test can fake the Plex-write mechanism without a
#: live Plex server, the same seam ``remux_runner`` gives the no-Plex
#: mechanism below.
PlexDefaultAudioRunner = Callable[..., PlexDefaultAudioResult]

#: :class:`~collapsarr.plex.default_audio_write.PlexDefaultAudioOutcome` members that
#: represent a genuine failure -- everything except ``SUCCESS``/``NOTHING_TO_DO``
#: (handled separately below). All collapse to the same
#: :attr:`~collapsarr.downmix.pipeline.PipelineOutcome.APPLY_FAILED` -- see the
#: module docstring's "Adapts ..." paragraph for why.
_PLEX_FAILURE_OUTCOME = PipelineOutcome.APPLY_FAILED


def _pipeline_result_from_plex(result: PlexDefaultAudioResult) -> PipelineResult:
    """Adapt a Plex-write outcome into the ``PipelineResult`` shape ``job.result`` expects."""
    if result.outcome is PlexDefaultAudioOutcome.SUCCESS:
        outcome = PipelineOutcome.SUCCESS
    elif result.outcome is PlexDefaultAudioOutcome.NOTHING_TO_DO:
        outcome = PipelineOutcome.NOTHING_TO_DO
    else:
        outcome = _PLEX_FAILURE_OUTCOME
    return PipelineResult(outcome=outcome, success=result.success, detail=result.detail)


def make_default_audio_pipeline_runner(
    session_factory: sessionmaker[Session],
    *,
    plex_runner: PlexDefaultAudioRunner = apply_default_audio_via_plex,
    remux_runner: DefaultAudioPipelineRunner = run_default_audio_pipeline,
    transport: httpx.BaseTransport | None = None,
) -> DefaultAudioPipelineRunner:
    """Build the mechanism-selection gate as a ``JobQueue``-compatible runner (COL-247).

    The returned callable is a drop-in
    :data:`~collapsarr.jobs.queue.DefaultAudioPipelineRunner` --
    :class:`~collapsarr.jobs.queue.JobQueue` calls it exactly as it would call
    the bare :func:`~collapsarr.downmix.default_audio_pipeline.
    run_default_audio_pipeline` (``(file_path, preference, **kwargs) ->
    PipelineResult``); it never sees a difference.

    On every call: opens a fresh :class:`~sqlalchemy.orm.Session` (see the
    module docstring for why), reads the singleton
    :class:`~collapsarr.plex.models.PlexConnection` row, and branches on
    :attr:`~collapsarr.plex.models.PlexConnection.is_configured`:

    - **Configured**: calls ``plex_runner`` (default
      :func:`~collapsarr.plex.default_audio_write.apply_default_audio_via_plex`)
      with this same session, adapts its result via
      :func:`_pipeline_result_from_plex`, and returns -- the session closes as
      this branch returns, right after the (fast, HTTP-only) Plex call.
      ``remux_runner`` is never invoked on this branch.
    - **Not configured**: closes the session *first*, then calls
      ``remux_runner`` (default :func:`~collapsarr.downmix.
      default_audio_pipeline.run_default_audio_pipeline`) with every keyword
      argument :class:`~collapsarr.jobs.queue.JobQueue` forwards
      (``cancel_handle``, and -- when configured -- ``ffmpeg_path``) and
      returns its :class:`~collapsarr.downmix.pipeline.PipelineResult`
      unchanged. ``plex_runner`` is never invoked on this branch.

    Exactly one branch ever runs per call -- there is no path where both
    ``plex_runner`` and ``remux_runner`` execute for the same file (COL-247's
    core acceptance criterion).

    ``plex_runner``/``remux_runner`` are the test seam (mirrors
    :meth:`~collapsarr.jobs.queue.JobQueue.__init__`'s own injectable
    runners): a test constructs this with recording fakes for either or both
    and asserts exactly one is invoked per ``PlexConnection`` state, with no
    live Plex server or ffmpeg/ffprobe binary required.
    """

    def runner(
        file_path: str | Path, preference: DefaultAudioPreference, **kwargs: object
    ) -> PipelineResult:
        with session_factory() as session:
            connection = get_plex_connection(session)
            if connection.is_configured:
                plex_result = plex_runner(
                    session,
                    file_path,
                    preference,
                    base_url=connection.base_url,
                    token=connection.token,
                    transport=transport,
                )
                return _pipeline_result_from_plex(plex_result)
        # Not configured: the session above is already closed -- a remux can
        # run for up to an hour (`remux_timeout`) shelling out to ffmpeg, and
        # this gate must never hold a DB session open for that whole duration.
        return remux_runner(file_path, preference, **kwargs)

    return runner
