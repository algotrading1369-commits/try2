"""Entrada: python -m ogame_bot [--config config.yaml] [--check-config]."""
from __future__ import annotations

import argparse
import asyncio
import logging
import json
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, load_config
from .errors import BrowserClosed, CoreError


def setup_logging(config: Config) -> None:
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(), RotatingFileHandler(
        config.log_file, maxBytes=config.rotation_mb * 1024 * 1024,
        backupCount=config.backup_count, encoding="utf-8",
    )]
    logger = logging.getLogger("ogame_bot")
    logger.setLevel(logging.INFO)
    for previous in logger.handlers[:]:
        logger.removeHandler(previous)
        previous.close()
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False


def main() -> int:
    parser = argparse.ArgumentParser(description="OGame: sesión, scanner y mejoras configurables; simulación por defecto.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yaml")
    parser.add_argument("--check-config", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scan-once", action="store_true", help="Lee una instantánea y cierra el navegador.")
    mode.add_argument("--session-only", action="store_true", help="Ejecuta solo el módulo de sesión.")
    mode.add_argument("--brain-once", action="store_true", help="Escanea y evalúa construcción/investigación una vez; respeta dry_run.")
    mode.add_argument("--scanner-only", action="store_true", help="Lee periódicamente sin evaluar ni ejecutar mejoras.")
    mode.add_argument("--fleet-once", action="store_true", help="Escanea y evalúa flotas una vez; respeta dry_run.")
    mode.add_argument("--defender-once", action="store_true", help="Comprueba defensa una vez; respeta dry_run.")
    mode.add_argument("--defender-only", action="store_true", help="Monitoriza defensa sin ejecutar módulos económicos.")
    mode.add_argument("--scheduler-status", action="store_true", help="Muestra el último estado del scheduler sin abrir el navegador.")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.check_config:
            print(f"Configuración válida: universo {config.server_number}, comunidad {config.community}.")
            return 0
        if args.scheduler_status:
            from .scheduler.loop import read_status
            print(json.dumps(read_status(config.scheduler), ensure_ascii=False, indent=2))
            return 0
        setup_logging(config)
        from .app import run
        asyncio.run(run(config, scan_once=args.scan_once, session_only=args.session_only, scanner_only=args.scanner_only, brain_once=args.brain_once, fleet_once=args.fleet_once, defender_once=args.defender_once, defender_only=args.defender_only))
    except BrowserClosed:
        logging.getLogger("ogame_bot").info("Navegador cerrado. Sesión terminada.")
        return 0
    except KeyboardInterrupt:
        print("Sesión terminada.")
        return 0
    except CoreError as error:
        print(f"Detenido: {error}")
        return 1
    except (ImportError, OSError):
        print("No se pudo iniciar: comprueba dependencias, permisos y navegador instalado.")
        return 1
    except Exception as error:
        # Los errores originales de Playwright pueden incluir URLs con tokens.
        print(f"Error técnico ({type(error).__name__}); sesión detenida.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
