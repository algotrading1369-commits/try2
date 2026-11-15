"""Loopback panel and child-process supervision for a single Linux VPS account.

systemd supervises this parent. A persisted user intent distinguishes a crash
from a deliberate Stop and survives reboots without starting duplicate bots.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .errors import CoreError
from .panel import LocalPanelServer, handler_for
from .panel_service import PanelError, PanelService
from .storage.snapshots import save_snapshot

logger = logging.getLogger('ogame_bot.vps')
MODES = {'full', 'scanner', 'session', 'defender'}


class VPSPanelService(PanelService):
    def __init__(self, config_path: Path) -> None:
        super().__init__(config_path)
        self.intent_path = self.config.scheduler.status_file.with_name('vps_intent.json')
        self.desired_running = False
        self.desired_mode = 'full'
        self.next_restart = 0.0
        self.restart_failures = 0
        self.child_started_at: float | None = None
        self.last_exit_reported = False
        if self.intent_path.exists():
            try:
                if self.intent_path.stat().st_size > 2048:
                    raise ValueError('Intent too large')
                raw = json.loads(self.intent_path.read_text(encoding='utf-8'))
                if not isinstance(raw, dict) or raw.get('version') != 1 or type(raw.get('running')) is not bool or type(raw.get('mode')) is not str or raw['mode'] not in MODES:
                    raise ValueError('Invalid intent')
                self.desired_running, self.desired_mode = raw['running'], raw['mode']
            except (OSError, ValueError, UnicodeError):
                raise CoreError('No se pudo validar la intención de arranque del VPS; no se iniciará el bot.') from None

    def _intent(self, running: bool, mode: str) -> None:
        save_snapshot(self.intent_path, {'version': 1, 'running': running, 'mode': mode})
        self.desired_running, self.desired_mode = running, mode

    def start(self, mode: str = 'full') -> dict[str, str]:
        with self.lock:
            if mode not in MODES:
                raise PanelError('Modo de ejecución desconocido.')
            status = self.process_status()
            if status['owned_running'] or status['external_running']:
                raise PanelError('El bot ya está activo.', 409)
            self._intent(True, mode)
            self.next_restart = 0.0
            return self._launch()

    def _launch(self) -> dict[str, str]:
        result = super().start(self.desired_mode)
        self.child_started_at = time.monotonic()
        self.last_exit_reported = False
        return result

    def stop(self) -> dict[str, str]:
        with self.lock:
            # Persist Stop before signaling: a fast exit must not race restart.
            self._intent(False, self.desired_mode)
            status = self.process_status()
            if not status['owned_running'] and not status['external_running']:
                return {'message': 'Bot detenido. Se conservará la parada tras reiniciar el VPS.'}
            return super().stop()

    def supervise_once(self, now: float | None = None) -> None:
        with self.lock:
            current = time.monotonic() if now is None else now
            if not self.desired_running:
                return
            status = self.process_status()
            if status['owned_running'] or status['external_running']:
                if self.child_started_at is not None and current-self.child_started_at >= 600:
                    self.restart_failures = 0
                return
            if self.child_started_at is not None and not self.last_exit_reported:
                self.restart_failures += 1
                self.next_restart = current + min(300, 5 * 2 ** min(self.restart_failures-1, 6))
                self.last_exit_reported = True
                logger.warning('El proceso del bot terminó; se reintentará con espera acotada.')
            if current < self.next_restart:
                return
            try:
                self._launch()
            except (OSError, CoreError, PanelError):
                self.restart_failures += 1
                self.next_restart = current + min(300, 5 * 2 ** min(self.restart_failures-1, 6))
                logger.warning('No se pudo iniciar el bot; el supervisor volverá a intentarlo.')

    def shutdown_child(self) -> None:
        """Stop only this supervisor's child without changing persisted intent."""
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                return
            try:
                super().stop()
            except (OSError, PanelError):
                logger.warning('No se pudo solicitar la parada cooperativa del proceso hijo.')
        try:
            self.process.wait(timeout=35)
        except (subprocess.TimeoutExpired, OSError):
            # systemd KillMode=mixed cleans remaining browser processes after
            # giving the parent time to request cooperative child shutdown.
            logger.warning('La parada del hijo agotó su plazo; systemd cerrará el grupo de procesos.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Supervisor y panel privado para VPS Linux.')
    parser.add_argument('--config', type=Path, default=Path('/var/lib/ogame/config.yaml'))
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Puerto no válido.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        service = VPSPanelService(args.config)
        with LocalPanelServer(('127.0.0.1', args.port), handler_for(service)) as server:
            server.timeout = 1
            logger.info('Panel privado del VPS disponible en 127.0.0.1:%s.', args.port)
            try:
                while not stop.is_set():
                    server.handle_request()
                    service.supervise_once()
            finally:
                service.shutdown_child()
    except (CoreError, OSError, ValueError):
        logger.error('No se pudo iniciar el supervisor. Comprueba configuración, permisos y puerto.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
