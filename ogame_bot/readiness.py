"""Diagnóstico local: configuración y últimas lecturas, sin navegador ni envíos."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .domain.models import Reading
from .fleet.models import SHIP_IDS


def load_state(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 5_000_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, UnicodeError, ValueError):
        return None


def fresh_data(raw: Any, max_age: float, now: datetime) -> Any:
    if not isinstance(raw, dict):
        return None
    reading = Reading(raw.get("data"), raw.get("observed_at", ""), raw.get("status", "unavailable"))
    return reading.data if reading.is_fresh(max_age, now) else None


def diagnose(config: Config, snapshot: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    checks: list[dict[str, str]] = []

    def add(code: str, state: str, message: str) -> None:
        checks.append({"code": code, "state": state, "message": message})

    if config.brain.economic_live:
        message = "Edificios e investigación con recursos reales según objetivos; flotas en simulación." if config.brain.dry_run else "Edificios, investigación y flotas habilitados en modo real."
    elif config.brain.probe_bootstrap_live:
        message = "Preparación REAL limitada: requisitos y una sonda. Flotas en simulación." if config.brain.dry_run else "Preparación real de sonda y envíos habilitados; operación autónoma sin verificar."
    else:
        message = "Simulación activa: no se envían órdenes." if config.brain.dry_run else "Envíos habilitados en configuración; esto no prueba su funcionamiento."
    add("execution_mode", "info", message)
    if config.brain.shipyard_enabled:
        add("shipyard_mode", "info", "Hangar: fabricación REAL habilitada." if config.brain.shipyard_live else "Hangar: simulación de fabricación.")
    if config.fleet.enabled and config.fleet.expeditions["enabled"]:
        add("expedition_mode", "info", "Expediciones: envíos REALES habilitados." if config.fleet.expeditions.get("live",False) else "Expediciones: simulación de envíos.")
    capacity_required = config.defender.enabled or (config.fleet.enabled and (config.fleet.transports["enabled"] or config.fleet.recycling["enabled"] or (config.fleet.expeditions["enabled"] and config.fleet.expeditions.get("composition_mode","automatic") == "automatic")))
    if capacity_required:
        if not config.fleet.capacities:
            add("ship_capacities", "blocked", "Faltan capacidades de carga verificadas en fleet.capacities.")
    if config.defender.enabled and config.defender.mission in {3, 4} and not config.defender.destinations:
        add("own_destination", "blocked", "Faltan destinos propios en defender.destinations.")
    if config.brain.builder_enabled and config.brain.strategy == "priority" and not config.brain.build_goals:
        add("building_goals", "blocked", "Construcción habilitada sin objetivos de prioridad.")
    if config.brain.research_enabled and not config.brain.research_goals:
        add("research_goals", "blocked", "Investigación habilitada sin objetivos configurados.")
    for key, title in (("expeditions", "Expediciones"), ("transports", "Transportes"), ("recycling", "Reciclaje")):
        section = getattr(config.fleet, key)
        if not config.fleet.enabled or not section["enabled"]:
            add(key, "disabled", title + ": desactivado en configuración.")

    if snapshot is None:
        add("snapshot", "pending", "No hay una instantánea legible; hace falta una lectura nueva.")
    elif snapshot.get("player") != config.expected_player_name or snapshot.get("server") != config.server_origin:
        add("snapshot_identity", "blocked", "La instantánea pertenece a otra cuenta o servidor; no se utiliza.")
    else:
        age_limit = config.scanner_max_reading_age_seconds
        counters = fresh_data(snapshot.get("fleet_slots"), age_limit, now)
        if isinstance(counters, dict) and all(type(counters.get(k)) is int for k in ("fleet_used", "fleet_total")) and 0 <= counters["fleet_used"] <= counters["fleet_total"]:
            free = counters["fleet_total"] - counters["fleet_used"]
            add("fleet_slots", "ok" if free else "blocked", f"Slots de flota libres en la última lectura vigente: {free}.")
        else:
            add("fleet_slots", "pending", "Slots desconocidos o caducados; hace falta una lectura nueva.")
        bodies = snapshot.get("bodies")
        for body in bodies if isinstance(bodies, list) else []:
            if not isinstance(body, dict) or not isinstance(body.get("body"), dict):
                continue
            identifier = body["body"].get("id")
            if type(identifier) is not int:
                continue
            prefix = f"body_{identifier}"
            ships = fresh_data(body.get("ships"), age_limit, now)
            resources = fresh_data(body.get("resources"), age_limit, now)
            if not isinstance(ships, dict) or any(type(ships.get(ship)) is not int or ships[ship] < 0 for ship in SHIP_IDS):
                add(prefix + "_inventory", "pending", f"Cuerpo {identifier}: inventario desconocido, incompleto o caducado.")
                continue
            if config.defender.enabled and (not config.defender.origins or identifier in config.defender.origins):
                if config.defender.mission == 6:
                    if ships["espionage_probe"] < 1:
                        add(prefix + "_probe", "blocked", f"Cuerpo {identifier}: falta una sonda para espionaje a posición 16.")
                    if ships["small_cargo"] + ships["large_cargo"] < 1:
                        add(prefix + "_cargo", "blocked", f"Cuerpo {identifier}: falta una carguera.")
                missing = [ship for ship, amount in ships.items() if ship in SHIP_IDS and amount > 0 and ship not in config.fleet.capacities and not (config.defender.mission == 6 and ship == "espionage_probe")]
                if missing and config.fleet.capacities:
                    add(prefix + "_capacities", "blocked", f"Cuerpo {identifier}: faltan capacidades para " + ", ".join(sorted(missing)) + ".")
                if isinstance(resources, dict) and type(resources.get("deuterium")) is int:
                    if config.defender.maximum_fuel > resources["deuterium"]:
                        add(prefix + "_fuel", "blocked", f"Cuerpo {identifier}: la reserva de combustible configurada supera el deuterio disponible. No es una cotización de consumo real.")
                else:
                    add(prefix + "_resources", "pending", f"Cuerpo {identifier}: recursos desconocidos o caducados.")

    add("live_fleet_validation", "pending", "Pendiente: formulario real, consumo/duración y confirmación de un envío y su regreso.")
    add("sustained_operation", "pending", "Pendiente: prueba prolongada y recuperación de sesiones en vivo.")
    return {"checked_at": now.isoformat(), "dry_run": config.brain.dry_run,
            "autonomous_operation": "unverified", "checks": checks}


def format_report(report: dict[str, Any]) -> str:
    labels = {"blocked": "BLOQUEO", "pending": "PENDIENTE", "ok": "OK", "info": "INFO", "disabled": "DESACTIVADO"}
    lines = ["Diagnóstico local del bot. La operación autónoma sigue sin verificar."]
    for check in report["checks"]:
        lines.append(f"[{labels[check['state']]}] {check['message']}")
    return "\n".join(lines)
