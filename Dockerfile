# Collapsarr container image (COL-39).
#
# Multi-stage, multi-arch (linux/amd64 + linux/arm64) build:
#   1. `frontend`  — Node stage that builds the Vite/React UI into /frontend/dist.
#   2. `builder`   — Python stage that builds the collapsarr wheel via hatchling,
#                    bundling the frontend stage's output into it (COL-40).
#   3. final       — python:3.12-slim + FFmpeg that installs the wheel (UI
#                    included) and runs as a PUID/PGID-adjustable user
#                    (linuxserver.io convention).
#
# Build for one arch locally:
#   docker build -t collapsarr:dev .
# Build/push multi-arch via buildx:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <repo>/collapsarr:tag --push .

# ---- Stage 1: build the frontend ------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /frontend

# Install deps against the lockfile first so this layer caches across source edits.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---- Stage 2: build the Python wheel -------------------------------------------
FROM python:3.12-slim AS builder
WORKDIR /build

RUN pip install --no-cache-dir build

# pyproject reads README.md (readme) and LICENSE (license-files) at build time.
# hatch_build.py is the custom build hook that bundles frontend/dist into the
# wheel as collapsarr/static (COL-40) — it requires frontend/dist to exist,
# hence copying the frontend stage's output in before building.
COPY pyproject.toml README.md LICENSE hatch_build.py ./
COPY collapsarr/ ./collapsarr/
COPY --from=frontend /frontend/dist ./frontend/dist

RUN python -m build --wheel --outdir /dist

# ---- Stage 3: runtime image ----------------------------------------------------
FROM python:3.12-slim

# FFmpeg is the core dependency (downmix pipeline); gosu drops privileges to the
# PUID/PGID user at start-up. --no-install-recommends + apt-list cleanup keep the
# layer small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root run user (linuxserver.io "abc"); the entrypoint remaps it to the
# requested PUID/PGID at container start.
RUN groupadd -g 1000 abc \
    && useradd -o -m -u 1000 -g abc -d /config -s /usr/sbin/nologin abc

# Install the app from the wheel built in the previous stage. The wheel already
# bundles the built UI as collapsarr/static (COL-40) and FastAPI serves it
# directly from there — no separate copy of the frontend build needed here.
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Runtime configuration. COLLAPSARR_* mirror the app defaults (collapsarr/config.py);
# PUID/PGID are consumed by the entrypoint. COLLAPSARR_DATA_DIR points at the
# /config volume; database_path derives to /config/collapsarr.db (COL-46), so
# existing /config volumes keep landing the DB at the same path on upgrade.
ENV COLLAPSARR_HOST=0.0.0.0 \
    COLLAPSARR_PORT=8282 \
    COLLAPSARR_DATA_DIR=/config \
    PUID=1000 \
    PGID=1000

# Persistent data (SQLite DB + config) lives here.
VOLUME /config

EXPOSE 8282

# Liveness probe using the stdlib so no extra packages are needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8282/health').status==200 else 1)"

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["collapsarr"]
