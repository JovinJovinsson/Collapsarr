#!/bin/sh
# Collapsarr container entrypoint (COL-39).
#
# Applies the linuxserver.io PUID/PGID convention: remap the bundled "abc"
# user/group to the host-supplied ids so files written to mounted volumes (the
# downmixed tracks and the /config data dir) land with the desired ownership,
# then drop privileges via gosu before exec'ing the app.
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Remap the run user/group to the requested ids (-o allows non-unique ids).
groupmod -o -g "$PGID" abc
usermod -o -u "$PUID" abc

# Ensure the writable data dir exists and is owned by the run user.
mkdir -p /config
chown -R abc:abc /config

echo "Collapsarr starting as uid=${PUID} gid=${PGID}"

# Drop privileges and hand off to the app (CMD, default: collapsarr).
exec gosu abc "$@"
