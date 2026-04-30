#!/usr/bin/env bash
set -euo pipefail

A="homeassistant-addon/ups2mqtt/app/ups2mqtt"
B="ups2mqtt/rootfs/usr/src/app/ups2mqtt"

if diff -qr --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' "$A" "$B" >/dev/null; then
  echo "Runtime trees are in sync"
  exit 0
fi

echo "Runtime trees differ. Run scripts/sync_runtime_tree.sh" >&2
diff -qr --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' "$A" "$B" || true
exit 1
