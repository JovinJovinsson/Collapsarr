# Collapsarr

An *arr family app to collapse audio channels into simpler configurations.

Collapsarr detects monitored media files that are missing a lower-channel-count
audio track (e.g. a stereo downmix of a 5.1/7.1 source) and adds one via FFmpeg,
without ever touching or replacing the original track.

> Status: early development. This repository currently contains the backend
> skeleton (FastAPI app, SQLite persistence layer, configuration, tests).

## Requirements

- Python 3.12+
- FFmpeg (external system dependency, checked at startup — not bundled)

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

## License

[GPLv3](LICENSE).
