#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
: "${XAUTHORITY:?XAUTHORITY debe estar definido por systemd}"
if [[ ${XAUTHORITY} != /run/ogame-display/Xauthority ]]; then
  echo 'Ruta de autorización X11 no válida.' >&2
  exit 1
fi
touch -- "${XAUTHORITY}"
chmod 0600 -- "${XAUTHORITY}"
printf 'add :99 . %s\n' "$(mcookie)" | xauth -f "${XAUTHORITY}" source -
exec /usr/bin/Xvfb :99 -screen 0 1280x800x24 -nolisten tcp -auth "${XAUTHORITY}"
