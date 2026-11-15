"""Private local backup of configuration, state and consistent SQLite orders.

Browser profiles, cookies, logs and credentials are deliberately outside the
archive allowlist. A local backup is not an off-server backup.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, load_config
from .errors import CoreError


def make_backup(config: Config, destination: Path, *, keep: int = 7) -> Path:
    if config.source_file is None or keep < 1:
        raise CoreError('Configuración de copia no válida.')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = destination.resolve()
    base = config.source_file.parent.resolve()
    sources = {'config.yaml': config.source_file, 'orders.sqlite3': config.brain.journal_file,
        'state.json': config.scanner_output_file, 'defender_state.json': config.defender.report_file,
        'plan.json': config.brain.plan_file, 'fleet_plan.json': config.fleet.plan_file,
        'vps_intent.json': config.scheduler.status_file.with_name('vps_intent.json')}
    for path in sources.values():
        if path.is_symlink() or not path.resolve().is_relative_to(base):
            raise CoreError('La copia solo admite archivos locales de la instalación.')
    name = 'orbit-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8] + '.tar.gz'
    output = destination / name
    try:
        with tempfile.TemporaryDirectory(dir=destination, prefix='.backup-') as temporary:
            work = Path(temporary)
            for label, path in sources.items():
                if not path.is_file():
                    continue
                if label == 'orders.sqlite3':
                    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as source_db, closing(sqlite3.connect(work / label)) as target_db:
                        source_db.backup(target_db)
                else:
                    shutil.copyfile(path, work / label)
            archive = work / name
            with tarfile.open(archive, 'w:gz') as target:
                for path in work.iterdir():
                    if path != archive:
                        target.add(path, arcname=path.name, recursive=False)
            archive.chmod(0o600)
            os.replace(archive, output)
        existing = sorted((p for p in destination.glob('orbit-*.tar.gz') if p.is_file() and not p.is_symlink()), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        for old in existing[keep:]:
            old.unlink()
    except (OSError, sqlite3.Error, tarfile.TarError):
        raise CoreError('No se pudo completar la copia privada del VPS.') from None
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description='Copia privada de estado y órdenes, sin perfiles ni credenciales.')
    parser.add_argument('--config', type=Path, default=Path('/var/lib/ogame/config.yaml'))
    parser.add_argument('--destination', type=Path, default=Path('/var/backups/ogame'))
    args = parser.parse_args()
    try:
        make_backup(load_config(args.config), args.destination)
        print('Copia local completada; conservación de siete archivos.')
    except CoreError as error:
        print(str(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
