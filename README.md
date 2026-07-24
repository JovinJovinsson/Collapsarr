<img src="frontend/public/favicon.svg" width="72" align="right" alt="Collapsarr logo" />

# Collapsarr

**Never get stuck without a downmix again.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-8B5CF6.svg)](LICENSE)
[![CI](https://github.com/JovinJovinsson/Collapsarr/actions/workflows/release.yml/badge.svg)](https://github.com/JovinJovinsson/Collapsarr/actions/workflows/release.yml)
[![Docker](https://img.shields.io/docker/v/odxnsson/collapsarr?label=docker)](https://hub.docker.com/r/odxnsson/collapsarr)
[![PyPI](https://img.shields.io/pypi/v/collapsarr)](https://pypi.org/project/collapsarr/)
[![Release](https://img.shields.io/github/v/release/JovinJovinsson/Collapsarr)](https://github.com/JovinJovinsson/Collapsarr/releases)

Collapsarr is a companion application for Sonarr and Radarr. It watches your
library for media missing a lower-channel-count audio track — a 7.1 release
with no stereo fallback, a 5.1 file your soundbar can't decode — and adds one
automatically via FFmpeg, without touching the track that's already there.

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

**Docker (recommended):**

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

Then open `http://localhost:8282` — you'll land on a one-time credential setup
page (see [Authentication](#authentication) for how login is enforced,
including the caveat if you're putting Collapsarr behind a reverse proxy).
`restart: unless-stopped` above means the container comes back automatically
whenever it stops unexpectedly or the Docker daemon restarts (e.g. after a
host reboot) — see [Running on startup](#running-on-startup) if you need it
to survive a reboot on a bare-metal/PyPI install instead.

**PyPI (bare-metal):**

```bash
pipx install collapsarr
collapsarr
```

No flags, no config file needed — Collapsarr stores its SQLite database
under your platform's standard per-user data directory by default (e.g.
`~/.local/share/collapsarr/collapsarr.db` on Linux; native per-OS locations
on macOS/Windows), creating it automatically if it doesn't exist. Set
`COLLAPSARR_DATA_DIR` if you'd rather it live somewhere else — see
[Configuration](#configuration). Requires FFmpeg on `PATH` — see
[Requirements](#requirements) below. Open `http://localhost:8282`; see
[Configuration](#configuration) for the full list of environment variables,
and [Running on startup](#running-on-startup) for a systemd unit.

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

## Authentication

Collapsarr requires a one-time credential setup (`/setup`, first run) and,
after that, logging in (`/login`) before the UI/API is usable — *except* from
a caller Collapsarr considers "local". The **Login requirement** setting
(Settings → General, `auth_required` in the API) controls this:

| Mode | Behaviour |
| --- | --- |
| **Disabled for local addresses** (`local_bypass`, default) | A caller connecting from a loopback (`127.0.0.1`/`::1`) or private-range (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, etc.) address reaches the UI and API with no setup and no login. Anyone connecting from a routable/public address still has to authenticate normally. |
| **Always required** (`enabled`) | Every caller is challenged, regardless of address. |

**Reverse-proxy limitation:** local-address classification looks only at the
*direct* TCP connection Collapsarr accepted — never an `X-Forwarded-For` (or
similar) header, since that's supplied by the client and trivially spoofable.
If Collapsarr sits behind a reverse proxy (nginx, Traefik, Cloudflare Tunnel,
etc.), every request's direct peer is the proxy itself, which usually *is*
local — meaning **every** client, including ones out on the internet, would
be classified as local and skip authentication entirely. **If you run
Collapsarr behind a reverse proxy, set the Login requirement to "Always
required" (`auth_required=enabled`)** until a future release adds
trusted-proxy support (a stubbed-out capability today).

**Headless deploys — seeding a credential without the setup page:** a
declarative/automated deploy (Docker Compose, Ansible, etc.) has no human
available to click through `/setup`. Set `COLLAPSARR_AUTH_USERNAME` and
`COLLAPSARR_AUTH_PASSWORD` (together — see
[Configuration](#configuration)) and a fresh install seeds that credential on
first boot instead — hashed before it's persisted, never stored or logged in
plaintext — and comes up already past the setup gate. `COLLAPSARR_AUTH_METHOD`
and `COLLAPSARR_AUTH_REQUIRED` are honoured at the same time if set, otherwise
the seeded credential gets the same defaults `/setup` would (`forms`,
`local_bypass`).

This doubles as the supported password-recovery/lockout escape hatch, but
**only for a fresh or already-locked-out install with no credential
configured yet** — seeding runs once and never overwrites a credential that
already exists, even if the environment variables are still set on a later
boot. It does **not** help recover a *forgotten* password once a credential
is already set; that requires clearing the existing `auth_username`/
`auth_password_hash` first (e.g. directly in the database) so the instance
has no credential again, at which point re-seeding (or `/setup`) applies.

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
| `COLLAPSARR_DATA_DIR` | *(OS user-data dir)* | Root directory for application data — the SQLite database today, logs/backups later. Defaults to `platformdirs.user_data_dir("collapsarr")` (e.g. `~/.local/share/collapsarr` on Linux). Created automatically if missing. The Docker image sets this to `/config` (its mounted volume) — see [Quick start](#quick-start). |
| `COLLAPSARR_DATABASE_PATH` | *(derived from `COLLAPSARR_DATA_DIR`)* | SQLite database file path. Set this to override the location directly, independent of `COLLAPSARR_DATA_DIR`. |
| `COLLAPSARR_DATABASE_URL` | *(derived from path)* | Full SQLAlchemy URL override — takes precedence over both of the above. |
| `COLLAPSARR_HOST` | `0.0.0.0` | API server bind address. |
| `COLLAPSARR_PORT` | `8282` | API server bind port. |
| `COLLAPSARR_LOG_LEVEL` | `INFO` | Log level. |
| `COLLAPSARR_AUTH_USERNAME` | *(unset)* | First-boot credential seed: UI username. Set together with `COLLAPSARR_AUTH_PASSWORD` — see [Authentication](#authentication). |
| `COLLAPSARR_AUTH_PASSWORD` | *(unset)* | First-boot credential seed: UI password. Hashed before being persisted; never stored or logged in plaintext. |
| `COLLAPSARR_AUTH_METHOD` | *(unset — `forms`)* | Optional, only applied when the seed credential above is actually seeded: `forms` or `basic`. |
| `COLLAPSARR_AUTH_REQUIRED` | *(unset — `local_bypass`)* | Optional, only applied when the seed credential above is actually seeded: `enabled` or `local_bypass`. |

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
