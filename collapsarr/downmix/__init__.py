"""Downmix engine: channel-layout detection, target selection, FFmpeg remux (COL-15+).

Home for downmix-engine concerns per ``docs/TRACKER.md``: FFprobe-based
audio-stream inspection, no-upmix target detection, the FFmpeg remux itself,
and validate + atomic swap.
"""

from __future__ import annotations

from .probe import AudioStreamInfo, FfprobeError, FfprobeNotFoundError, probe_audio_streams
from .remux import RemuxResult, build_remux_command, run_remux
from .targets import (
    DownmixSettings,
    DownmixTarget,
    QualifyingTarget,
    detect_qualifying_targets,
)

__all__ = [
    "AudioStreamInfo",
    "DownmixSettings",
    "DownmixTarget",
    "FfprobeError",
    "FfprobeNotFoundError",
    "QualifyingTarget",
    "RemuxResult",
    "build_remux_command",
    "detect_qualifying_targets",
    "probe_audio_streams",
    "run_remux",
]
