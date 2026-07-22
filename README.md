<img src="frontend/public/favicon.svg" width="72" align="right" alt="Collapsarr logo" />

# Collapsarr

**Never get stuck without a downmix again.**

<!--
  Stub row: static "pending" badges, deliberately not pointed at real
  endpoints yet. Swap for live dynamic badges once COL-8 (Packaging &
  Release) ships a release.yml workflow and the DockerHub repo is live:
    CI      -> https://github.com/JovinJovinsson/Collapsarr/actions/workflows/release.yml/badge.svg
    Docker  -> https://img.shields.io/docker/pulls/odxnsson/collapsarr
    Release -> https://img.shields.io/github/v/release/JovinJovinsson/Collapsarr
-->
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-8B5CF6.svg)](LICENSE)
[![CI](https://img.shields.io/badge/CI-pending-lightgrey.svg)](https://github.com/JovinJovinsson/Collapsarr/actions)
[![Docker](https://img.shields.io/badge/docker-pending-lightgrey.svg)](https://hub.docker.com/r/odxnsson/collapsarr)
[![Release](https://img.shields.io/badge/release-pending-lightgrey.svg)](https://github.com/JovinJovinsson/Collapsarr/releases)

Collapsarr is a companion application for Sonarr and Radarr. It watches your
library for media missing a lower-channel-count audio track — a 7.1 release
with no stereo fallback, a 5.1 file your soundbar can't decode — and adds one
automatically via FFmpeg, without touching the track that's already there.

> **Status:** core backend and Web UI are built; packaging (PyPI + Docker) is
> in progress. Until that ships, run from source — see
> [Development](#development) below.

## Why

- **No upmixing, ever.** Collapsarr only adds tracks the source can actually
  support (a stereo/2.1/5.1 downmix from a higher channel count) — it will
  never fake a 5.1 track out of a stereo source.
- **Originals are never at risk.** The remux writes to a temp file, validates
  duration and stream count, then atomically swaps it in. Any failure at any
  stage leaves the original file completely untouched — no partial writes, no
  orphaned backups.
- **Fits into the \*arr stack you already run.** Sonarr/Radarr integration
  (webhooks + periodic scan), a dark UI in the same style as the rest of the
  family, and a REST API following the same conventions.

## Features

- Sonarr and Radarr integration — instance config, connectivity check, remote
  path mapping, multiple concurrent instances
- Per-target, per-language detection that stacks additional targets without
  duplicating what's already there
- FFmpeg remux: stream-copies existing tracks, encodes new audio (AAC for
  Stereo, AC3 @ 448kbps for 2.1/5.1)
- Job queue with configurable concurrency — triggered by webhook, periodic
  full-library scan, on-demand scan, or manual per-file trigger
- Full job history: status, timestamps, FFmpeg exit code, error text
- Web UI in the same dark theme as Sonarr/Radarr/Bazarr — Wanted view,
  Activity/History, per-file detail view with a manual trigger
- Webhook + Discord notifications on job failure or app health issues (e.g.
  FFmpeg missing)
- REST API with \*arr-convention auth (API key)

## Quick start

<!--
  Drafted now, finalized once COL-8 (Packaging & Release) ships: this image
  isn't published to Docker Hub yet, so this is the target shape, not a
  working pull today.
-->

```yaml
services:
  collapsarr:
    image: odxnsson/collapsarr:latest
    container_name: collapsarr
    ports:
      - "8282:8282"
    volumes:
      - ./config:/config
      - /path/to/media:/media
    environment:
      - PUID=1000
      - PGID=1000
    restart: unless-stopped
```

Then open `http://localhost:8282`. `restart: unless-stopped` above means the
container comes back automatically whenever it stops unexpectedly or the
Docker daemon restarts (e.g. after a host reboot) — see
[Running on startup](#running-on-startup) if you need it to survive a reboot
on a bare-metal/PyPI install instead.

## Requirements

- Python 3.12+
- FFmpeg — external system dependency, checked at startup and reported on the
  health page if missing. Bundled in the Docker image; install it yourself
  for a bare-metal/PyPI setup:

  | OS | Command |
  | --- | --- |
  | Debian / Ubuntu | `sudo apt install ffmpeg` |
  | Fedora | `sudo dnf install ffmpeg` |
  | Arch | `sudo pacman -S ffmpeg` |
  | macOS (Homebrew) | `brew install ffmpeg` |
  | Windows (winget) | `winget install ffmpeg` |

  Verify with `ffmpeg -version`. Official builds/source: [ffmpeg.org/download.html](https://ffmpeg.org/download.html).

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run the checks
pytest
ruff check .
mypy

# Run the server (defaults to http://0.0.0.0:8282)
python -m collapsarr
# then: curl http://localhost:8282/health
#   ->  {"status":"ok","version":"...","warnings":[]}
#   (or, if FFmpeg is missing: {"status":"degraded","version":"...",
#    "warnings":[{"code":"ffmpeg_missing","message":"..."}]})
```

## Configuration

All settings load from environment variables (prefixed `COLLAPSARR_`) with
sensible defaults, and an optional `.env` file is read from the working
directory. See [`.env.example`](.env.example).

| Variable | Default | Description |
| --- | --- | --- |
| `COLLAPSARR_DATABASE_PATH` | `/config/collapsarr.db` | SQLite database file path. |
| `COLLAPSARR_DATABASE_URL` | *(derived from path)* | Full SQLAlchemy URL override. |
| `COLLAPSARR_HOST` | `0.0.0.0` | API server bind address. |
| `COLLAPSARR_PORT` | `8282` | API server bind port. |
| `COLLAPSARR_LOG_LEVEL` | `INFO` | Log level. |

## Running on startup

**Docker:** the `restart: unless-stopped` line in the [Quick start](#quick-start)
compose file already handles this — Docker restarts the container whenever it
stops unexpectedly or the Docker daemon itself restarts. On Linux this
happens automatically on boot, since `dockerd` runs as a systemd service
enabled by default (`systemctl is-enabled docker` to confirm). On
Docker Desktop (macOS/Windows), enable **Settings → General → Start Docker
Desktop when you log in** so the daemon — and in turn the container — comes
up after a reboot.

**Bare-metal / PyPI install:** run Collapsarr as a systemd service so it
starts on boot and restarts if it crashes. Create
`/etc/systemd/system/collapsarr.service`:

```ini
[Unit]
Description=Collapsarr
After=network.target

[Service]
Type=simple
User=collapsarr
Group=collapsarr
WorkingDirectory=/opt/collapsarr
EnvironmentFile=/opt/collapsarr/.env
ExecStart=/opt/collapsarr/.venv/bin/collapsarr
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Adjust `User`/`Group`, `WorkingDirectory`, and the `.venv` path to match
where you installed it; `EnvironmentFile` should point at an `.env`
containing the `COLLAPSARR_*` variables from [Configuration](#configuration)
(see [`.env.example`](.env.example)). Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now collapsarr
```

Check status/logs with `systemctl status collapsarr` and `journalctl -u collapsarr -f`.

## Docs

Fuller docs live on the [GitHub Wiki](https://github.com/JovinJovinsson/Collapsarr/wiki).

## License

[GPLv3](LICENSE).
