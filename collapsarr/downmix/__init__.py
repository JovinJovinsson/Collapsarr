"""Downmix engine: channel-layout detection, target selection, FFmpeg remux (COL-15+).

Home for downmix-engine concerns per ``docs/TRACKER.md``: FFprobe-based
audio-stream inspection, no-upmix target detection, the FFmpeg remux itself,
and validate + atomic swap.
"""

from __future__ import annotations

from .apply import (
    DEFAULT_DURATION_TOLERANCE_SECONDS,
    ApplyFailureReason,
    ApplyResult,
    apply_remux_result,
)
from .default_audio import (
    DefaultAudioPreference,
    resolve_default_audio_output_index,
    resolve_default_audio_stream,
)
from .pipeline import PipelineOutcome, PipelineResult, run_downmix_pipeline
from .probe import (
    AudioStreamInfo,
    FfprobeError,
    FfprobeNotFoundError,
    MediaSummary,
    probe_audio_streams,
    probe_media_summary,
)
from .remux import RemuxResult, build_remux_command, run_remux
from .targets import (
    DownmixSettings,
    DownmixTarget,
    QualifyingTarget,
    detect_qualifying_targets,
)

__all__ = [
    "DEFAULT_DURATION_TOLERANCE_SECONDS",
    "ApplyFailureReason",
    "ApplyResult",
    "AudioStreamInfo",
    "DefaultAudioPreference",
    "DownmixSettings",
    "DownmixTarget",
    "FfprobeError",
    "FfprobeNotFoundError",
    "MediaSummary",
    "PipelineOutcome",
    "PipelineResult",
    "QualifyingTarget",
    "RemuxResult",
    "apply_remux_result",
    "build_remux_command",
    "detect_qualifying_targets",
    "probe_audio_streams",
    "probe_media_summary",
    "resolve_default_audio_output_index",
    "resolve_default_audio_stream",
    "run_downmix_pipeline",
    "run_remux",
]
