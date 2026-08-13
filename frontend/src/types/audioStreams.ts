/**
 * Types mirroring `GET /api/files/:id/audio-streams`'s response (COL-204,
 * `collapsarr/media/routes.py`'s `AudioStreamsResponse`/`AudioStreamOut`) --
 * kept in sync by hand since there's no shared schema generation yet.
 */

/**
 * One of a file's current audio streams, as live-probed via ffprobe on this
 * request -- never a stored/cached value. `is_default` mirrors ffprobe's
 * `disposition.default`: whether this stream currently carries the
 * container's Default Audio Track disposition.
 */
export interface AudioStream {
  index: number;
  codec: string;
  channels: number;
  channel_layout: string;
  language: string;
  is_default: boolean;
}

/**
 * `probeable` is `false` when the file couldn't be probed right now
 * (missing on disk, corrupt, ffprobe unavailable) -- `streams` is then
 * always empty and `error` carries a human-readable reason, so the file
 * detail page can degrade gracefully instead of crashing.
 */
export interface AudioStreamsResponse {
  probeable: boolean;
  error: string | null;
  streams: AudioStream[];
}
