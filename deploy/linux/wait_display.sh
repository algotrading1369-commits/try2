#!/usr/bin/env bash
set -Eeuo pipefail
for attempt in {1..60}; do
  if /usr/bin/xdpyinfo -display :99 >/dev/null 2>&1; then
    exit 0
  fi
  sleep 0.25
done
echo 'La pantalla virtual no está disponible.' >&2
exit 1
