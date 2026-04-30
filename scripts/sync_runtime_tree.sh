#!/usr/bin/env bash
set -euo pipefail

SRC="homeassistant-addon/ups2mqtt/app/ups2mqtt/"
DST="ups2mqtt/rootfs/usr/src/app/ups2mqtt/"

if [[ ! -d "$SRC" || ! -d "$DST" ]]; then
  echo "Missing source or destination directory" >&2
  exit 1
fi

rsync -a --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  "$SRC" "$DST"

echo "Synced runtime tree: $SRC -> $DST"
