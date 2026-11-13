"""Validación real de lectura, duración limitada y reinicios de sesión.

No ejecuta servicios de mejoras, envíos ni Telegram. Nunca conserva HTML o cookies
en los informes. El perfil del navegador sigue siendo privado y persistente.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters.ogame.reader import OGameReader
from .config import Config, load_config
from .core.browser import BrowserManager
from .core.session import SessionManager
from .errors import CoreError
from .fleet import parsing as fleet_parsing
from .scanner.dom import Document
from .scanner.service import Scanner
from .scheduler.loop import Scheduler
from .storage.snapshots import save_snapshot


def startup_probe() -> dict[str, Any]:
    """El timeout está fuera del bucle, que puede atascarse al crearse en Windows."""
    code = "import faulthandler; faulthandler.dump_traceback_later(4, exit=True); import asyncio; asyncio.run(asyncio.sleep(0)); faulthandler.cancel_dump_traceback_later(); print('ASYNCIO_OK')"
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "stage": "event_loop", "error_type": type(error).__name__}
    ok = result.returncode == 0 and "ASYNCIO_OK" in result.stdout
    return {"ok": ok, "stage": "event_loop", "exit_code": result.returncode,
            "local_socketpair_stalled": "_fallback_socketpair" in result.stderr and "accept" in result.stderr}


class ProbeReader(OGameReader):
    def __init__(self, session: SessionManager, probes: dict[str, Any]) -> None:
        super().__init__(session)
        self.probes = probes

    async def capture(self, component: str, body_id: int | None = None, *, expand_events: bool = False) -> Document:
        started = time.monotonic()
        key = component + (":" + str(body_id) if body_id else "")
        try:
            document = await super().capture(component, body_id, expand_events=expand_events)
            result: dict[str, Any] = {"state": "captured", "seconds": round(time.monotonic() - started, 3)}
            if component == "fleetdispatch":
                result["slots_status"] = fleet_parsing.slots(document).status
                # Son únicamente cantidades de controles conocidos en la etapa inicial.
                result["initial_form_controls"] = {identifier: len(document.by_id(identifier)) for identifier in ("continueToFleet2", "continueToFleet3", "galaxy", "system", "position", "sendFleet")}
                result["later_form_steps"] = "not_exercised"
            if component == "movement":
                reading = fleet_parsing.outgoing(document)
                result["outgoing_status"] = reading.status
                result["issues"] = list(reading.issues)
            self.probes[key] = result
            return document
        except Exception as error:
            self.probes[key] = {"state": "read_failed", "error_type": type(error).__name__, "seconds": round(time.monotonic() - started, 3)}
            raise


async def live_validation(config: Config, root: Path, report: dict[str, Any], minutes: float, restart_minutes: float) -> None:
    duration = minutes * 60
    started: float | None = None
    segment = 0
    report["state"] = "waiting_for_initial_login"
    report["not_tested"] = ["real_construction", "real_research", "real_fleet_submission", "real_defensive_evacuation", "telegram_delivery"]
    save_snapshot(root / "report.json", report)
    try:
        while started is None or time.monotonic() - started < duration:
            segment_config = replace(config, manual_login_timeout_seconds=config.manual_login_timeout_seconds if segment == 0 else 1)
            async with BrowserManager(segment_config) as browser:
                session = SessionManager(browser.require_context(), segment_config)
                await session.connect()
                if started is None:
                    started = time.monotonic()
                    report["measurement_started_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    report["successful_profile_restarts"] += 1
                report["state"] = "running_read_only"
                report["sessions_started"] += 1
                probes: dict[str, Any] = {}
                reader = ProbeReader(session, probes)
                scanner = Scanner(reader, segment_config)
                scheduler = Scheduler(segment_config.scheduler, watchdog=session._check_open)
                session.before_navigation = scheduler.checkpoint

                async def scan() -> None:
                    before = time.monotonic()
                    sample: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "segment": segment}
                    try:
                        snapshot = await scanner.scan()
                        await reader.capture("movement")
                        sample.update({"registry": snapshot.registry.status, "research": snapshot.research.status,
                            "movement_events": snapshot.movements.status,
                            "threat_status": (snapshot.movements.data or {}).get("threat_status", "unknown"),
                            "bodies": [{"id": body.body.id, "resources": body.resources.status, "buildings": body.buildings.status,
                                        "facilities": body.facilities.status, "ships": body.ships.status} for body in snapshot.bodies]})
                    except Exception as error:
                        sample["error_type"] = type(error).__name__
                        raise
                    finally:
                        sample["seconds"] = round(time.monotonic() - before, 3)
                        report["samples"].append(sample)
                        report["interface_probes"] = dict(probes)
                        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
                        save_snapshot(root / "report.json", report)

                scheduler.add("session", session.check, lambda: (config.health_check_seconds, config.health_check_seconds), immediate=False)
                scheduler.add("scanner", scan, lambda: (config.scanner_min_interval_seconds, config.scanner_max_interval_seconds))
                remaining = max(0, duration - (time.monotonic() - started))
                timer_finished = False

                async def stop_at_deadline() -> None:
                    nonlocal timer_finished
                    await asyncio.sleep(min(restart_minutes * 60, remaining))
                    timer_finished = True
                    scheduler.request_stop()

                timer = asyncio.create_task(stop_at_deadline())
                try:
                    await scheduler.run()
                except asyncio.CancelledError:
                    if not timer_finished:
                        raise
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                    session.before_navigation = None
                    report["scheduler_state"] = scheduler.state
            segment += 1
        report["state"] = "duration_completed_read_only"
    except asyncio.CancelledError:
        report["state"] = "interrupted"
        raise
    except Exception as error:
        report["state"] = "blocked_or_failed"
        report["error_type"] = type(error).__name__
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3) if started is not None else 0
        report["duration_reached"] = started is not None and time.monotonic() - started >= duration
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["complete_automation_validated"] = False
        observed = report["samples"]
        statuses = [sample.get(key) for sample in observed for key in ("registry", "research", "movement_events")]
        statuses += [body.get(key) for sample in observed for body in sample.get("bodies", []) for key in ("resources", "buildings", "facilities", "ships")]
        probe_statuses = [value.get(key) for value in report.get("interface_probes", {}).values() for key in ("slots_status", "outgoing_status") if key in value]
        report["interface_reading_validation"] = "not_observed" if not observed else "incomplete_or_incompatible" if any(sample.get("error_type") for sample in observed) or any(status not in {"complete", "not_applicable"} for status in statuses + probe_statuses) else "observed_read_paths_compatible"
        save_snapshot(root / "report.json", report)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba real de lectura; no envía mejoras, flotas o notificaciones.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yaml")
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--restart-minutes", type=float, default=15)
    parser.add_argument("--probe-only", action="store_true", help="Diagnostica el arranque sin abrir navegador.")
    args = parser.parse_args()
    if not 0 < args.minutes <= 1440 or not 0 < args.restart_minutes <= 1440:
        parser.error("La duración y el intervalo de reinicio deben estar entre 0 y 1440 minutos.")
    try:
        config = load_config(args.config)
        root = args.config.resolve().parent / "runtime" / "validation" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
        root.mkdir(parents=True, exist_ok=True)
        report: dict[str, Any] = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "state": "preflight",
            "requested_minutes": args.minutes, "restart_minutes": args.restart_minutes, "dry_run_forced": True,
            "samples": [], "sessions_started": 0, "successful_profile_restarts": 0, "complete_automation_validated": False,
            "interface_reading_validation": "not_started", "duration_reached": False, "elapsed_seconds": 0}
        report["startup_probe"] = startup_probe()
        if not report["startup_probe"]["ok"]:
            report["state"] = "blocked_before_browser"
            save_snapshot(root / "report.json", report)
            print("Bloqueado antes de abrir navegador: el bucle asíncrono no pudo arrancar.")
            print("Informe: " + str(root / "report.json"))
            return 2
        if args.probe_only:
            report["state"] = "startup_probe_passed"
            save_snapshot(root / "report.json", report)
            print("Arranque asíncrono correcto. Informe: " + str(root / "report.json"))
            return 0
        config = replace(config, brain=replace(config.brain, dry_run=True, journal_file=root / "unused-orders.sqlite3", plan_file=root / "unused-plan.json"),
            telegram=replace(config.telegram, enabled=False, state_file=root / "unused-alerts.json"),
            scanner_output_file=root / "state.json", scheduler=replace(config.scheduler, status_file=root / "scheduler.json"))
        print("Validación solo de lectura. Inicia sesión manualmente si el navegador lo solicita.")
        print("Informe: " + str(root / "report.json"))
        asyncio.run(live_validation(config, root, report, args.minutes, args.restart_minutes))
        print("Resultado: " + report["state"])
        return 0 if report["state"] == "duration_completed_read_only" else 1
    except KeyboardInterrupt:
        print("Prueba interrumpida; el informe conserva las observaciones recogidas.")
        return 130
    except (CoreError, OSError) as error:
        print("No se pudo preparar la validación: " + type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
