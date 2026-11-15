#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo 'Ejecuta con sudo desde SSH; la contraseña no se mostrará.' >&2
  exit 1
fi
cd /opt/ogame
exec /opt/ogame/.venv/bin/python -m ogame_bot.credentials configure --config /var/lib/ogame/config.yaml
