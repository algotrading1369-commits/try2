"""Prepare a Linux copy of configuration without copying profiles or passwords."""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import load_config
from .errors import CoreError


def prepare_config(source: Path, destination: Path) -> None:
    if destination.exists():
        raise CoreError('La configuración de destino ya existe; no se sobrescribirá.')
    temporary: str | None = None
    try:
        raw: Any = yaml.safe_load(source.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            raise CoreError('Configuración de origen no válida.')
        raw['browser'].update(headless=False, channel=None, profile_dir='runtime/browser-profile')
        raw['session']['auto_login'] = False
        raw['session'].setdefault('login_retry_seconds', 300)
        raw['telegram']['enabled'] = False
        for section, key, value in (
            ('logging', 'file', 'runtime/logs/session.log'),
            ('scanner', 'output_file', 'runtime/state.json'),
            ('automation', 'journal_file', 'runtime/orders.sqlite3'),
            ('automation', 'plan_file', 'runtime/plan.json'),
            ('fleet', 'plan_file', 'runtime/fleet_plan.json'),
            ('defender', 'report_file', 'runtime/defender_state.json'),
            ('telegram', 'state_file', 'runtime/alert_state.json'),
            ('scheduler', 'status_file', 'runtime/scheduler_state.json'),
        ):
            raw[section][key] = value
        # First launch is read-only/simulated until migration is checked.
        raw['automation'].update(dry_run=True, economic_live=False, probe_bootstrap_live=False)
        if 'shipyard' in raw:
            raw['shipyard']['live'] = False
        raw['defender']['live'] = False
        for name in ('expeditions', 'transports', 'recycling', 'fleetsave'):
            if name in raw['fleet']:
                raw['fleet'][name]['live'] = False
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent, suffix='.yaml', delete=False) as output:
            temporary = output.name
            yaml.safe_dump(raw, output, allow_unicode=True, sort_keys=False)
        load_config(Path(temporary))
        # Atomic no-overwrite publication, including concurrent invocations.
        if os.name == 'nt':
            # Windows rename refuses an existing destination atomically.
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError):
        raise CoreError('No se pudo preparar la configuración Linux.') from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Copia y adapta configuración para el VPS; sin sobrescritura.')
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        prepare_config(args.source, args.destination)
        print('Configuración Linux preparada en simulación. Comprueba identidad, diario de órdenes y acceso antes de activar módulos reales.')
    except CoreError as error:
        print(str(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
