#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo 'Ejecuta este visor con sudo desde SSH.' >&2
  exit 1
fi
if ! systemctl is-active --quiet ogame-display.service; then
  echo 'Primero inicia ogame.service.' >&2
  exit 1
fi
if [[ ! -f /var/lib/ogame/.vnc/passwd ]]; then
  echo 'Elige una contraseña para el visor VNC; no uses la de Gameforge.'
  runuser -u ogame -- env HOME=/var/lib/ogame x11vnc -storepasswd
fi
echo 'Visor disponible solo en localhost:5900. Accede mediante túnel SSH y cierra con Ctrl+C.'
exec runuser -u ogame -- env HOME=/var/lib/ogame /usr/bin/x11vnc -display :99 -auth /run/ogame-display/Xauthority -localhost -listen 127.0.0.1 -rfbport 5900 -rfbauth /var/lib/ogame/.vnc/passwd -forever -shared
