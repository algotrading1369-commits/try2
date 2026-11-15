#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
if [[ ${EUID} -ne 0 ]]; then
  echo 'Ejecuta: sudo bash deploy/linux/install.sh' >&2
  exit 1
fi
# This installer targets Ubuntu 24.04 LTS and systemd system services.
. /etc/os-release
if [[ ${ID} != ubuntu || ${VERSION_ID} != 24.04 ]]; then
  echo 'Este instalador requiere Ubuntu 24.04 LTS.' >&2
  exit 1
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_dir=$(cd -- "${script_dir}/../.." && pwd -P)
if [[ ! -f "${source_dir}/ogame_bot/vps.py" || ! -f "${source_dir}/requirements.txt" ]]; then
  echo 'No se encuentra el proyecto completo.' >&2
  exit 1
fi
if systemctl is-active --quiet ogame.service; then
  systemctl stop ogame.service
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip rsync xvfb xauth x11-utils x11vnc util-linux logrotate
if ! id ogame >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/ogame --create-home --shell /usr/sbin/nologin ogame
fi
install -d -m 0755 /opt/ogame
install -d -o ogame -g ogame -m 0700 /var/lib/ogame /var/lib/ogame/runtime
if [[ "${source_dir}" != /opt/ogame ]]; then
  rsync -a --exclude='.git/' --exclude='.venv/' --exclude='runtime/' --exclude='config.yaml' --exclude='__pycache__/' "${source_dir}/" /opt/ogame/
fi
chown -R root:root /opt/ogame
chmod -R go-w /opt/ogame
chmod -R a+rX /opt/ogame
python3 -m venv /opt/ogame/.venv
/opt/ogame/.venv/bin/python -m pip install -r /opt/ogame/requirements.txt
/opt/ogame/.venv/bin/python -m playwright install-deps chromium
install -d -m 0755 /opt/ogame/browsers
PLAYWRIGHT_BROWSERS_PATH=/opt/ogame/browsers /opt/ogame/.venv/bin/python -m playwright install chromium
chmod -R a+rX /opt/ogame/browsers /opt/ogame/.venv
install -m 0644 /opt/ogame/deploy/linux/ogame.service /etc/systemd/system/ogame.service
install -m 0644 /opt/ogame/deploy/linux/ogame-display.service /etc/systemd/system/ogame-display.service
install -m 0644 /opt/ogame/deploy/linux/ogame-backup.service /etc/systemd/system/ogame-backup.service
install -m 0644 /opt/ogame/deploy/linux/ogame-backup.timer /etc/systemd/system/ogame-backup.timer
install -m 0644 /opt/ogame/deploy/linux/logrotate.conf /etc/logrotate.d/ogame
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/ogame.service /etc/systemd/system/ogame-display.service
echo 'Instalación preparada; todavía no se ha iniciado el bot.'
echo 'Sigue VPS_LINUX.md: configuración, diario de órdenes y credenciales antes de activar el servicio.'
