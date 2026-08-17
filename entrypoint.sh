#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID" in ''|*[!0-9]*) echo "PUID muss numerisch sein." >&2; exit 1 ;; esac
case "$PGID" in ''|*[!0-9]*) echo "PGID muss numerisch sein." >&2; exit 1 ;; esac

mkdir -p "${DATA_ROOT:-/data}" "${MEDIA_ROOT:-/media}"
chown -R "$PUID:$PGID" "${DATA_ROOT:-/data}"

export HOME="${DATA_ROOT:-/data}"
exec gosu "$PUID:$PGID" "$@"
