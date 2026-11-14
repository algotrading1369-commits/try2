"""Ajustes de flotas y contrato de selectores editable sin modificar Python."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError
from .models import SHIP_IDS

SELECTOR_KEYS = {"ship_input", "continue_2", "continue_3", "galaxy", "system", "position", "target_type", "speed", "mission", "holding", "resource_input", "fuel", "capacity", "flight_seconds", "send"}


@dataclass(frozen=True)
class FleetSettings:
    enabled: bool
    plan_file: Path
    min_interval_seconds: float
    max_interval_seconds: float
    reserved_slots: int
    deuterium_reserve: int
    maximum_fuel: int
    maximum_oneway_seconds: int
    speed_percent: int
    capacities: dict[str, int]
    expeditions: dict[str, Any]
    transports: dict[str, Any]
    recycling: dict[str, Any]
    selectors: dict[str, str]


def load_fleet_settings(raw: Any, base: Path) -> FleetSettings:
    keys = {"enabled", "plan_file", "min_interval_seconds", "max_interval_seconds", "reserved_slots", "deuterium_reserve", "maximum_fuel", "maximum_oneway_seconds", "speed_percent", "capacities", "expeditions", "transports", "recycling", "selectors"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ConfigurationError("Campos incorrectos en fleet.")

    def integer(value: Any, name: str, minimum: int = 0) -> int:
        if type(value) is not int or value < minimum:
            raise ConfigurationError(f"Entero inválido: fleet.{name}.")
        return value

    def boolean(value: Any, name: str) -> bool:
        if type(value) is not bool:
            raise ConfigurationError(f"Booleano inválido: fleet.{name}.")
        return value

    def number(value: Any, name: str, minimum: float) -> float:
        if type(value) not in (float, int) or not math.isfinite(value) or value < minimum:
            raise ConfigurationError(f"Número inválido: fleet.{name}.")
        return float(value)

    def ids(value: Any, name: str) -> None:
        if not isinstance(value, list) or any(type(item) is not int or item <= 0 for item in value) or len(set(value)) != len(value):
            raise ConfigurationError(f"Lista de orígenes inválida: {name}.")

    subkeys = {
        "expeditions": {"enabled", "origins", "top1_source", "top1_points", "top1_observed_at", "top1_max_age_seconds", "base_cap_override", "economy_speed", "player_class", "resource_bonus_percent", "holding_hours", "cargo_ship", "pathfinder", "probe", "combat_ship", "max_parallel"},
        "transports": {"enabled", "sources", "destination_id", "cargo_ship", "minimum_utilization_percent", "leave_resources"},
        "recycling": {"enabled", "origins", "radius_systems", "minimum_debris", "max_systems_per_cycle"},
    }
    for name, expected in subkeys.items():
        optional = {"live", "composition_mode", "manual_ships"} if name == "expeditions" else set()
        if not isinstance(raw[name], dict) or not expected <= set(raw[name]) or set(raw[name]) - expected - optional:
            raise ConfigurationError(f"Campos incorrectos en fleet.{name}.")
        boolean(raw[name]["enabled"], name + ".enabled")
    expo, transport, recycling = (dict(raw[name]) for name in ("expeditions", "transports", "recycling"))
    expo.setdefault("live", False)
    expo.setdefault("composition_mode", "automatic")
    expo.setdefault("manual_ships", {})
    boolean(expo["live"], "expeditions.live")
    if expo["composition_mode"] not in {"automatic", "manual"} or not isinstance(expo["manual_ships"], dict) or any(ship not in SHIP_IDS or type(amount) is not int or not 1 <= amount <= 1000000000 for ship, amount in expo["manual_ships"].items()):
        raise ConfigurationError("Composición de expedición inválida.")
    if expo["enabled"] and expo["composition_mode"] == "manual" and not expo["manual_ships"]:
        raise ConfigurationError("La expedición manual necesita al menos una nave.")
    ids(expo["origins"], "expeditions.origins")
    ids(transport["sources"], "transports.sources")
    ids(recycling["origins"], "recycling.origins")
    if expo["top1_source"] not in {"manual", "public_api"}:
        raise ConfigurationError("top1_source debe ser manual o public_api.")
    for name in ("top1_points", "base_cap_override"):
        if expo[name] is not None:
            integer(expo[name], name, 1)
    if expo["top1_observed_at"] is not None:
        if not isinstance(expo["top1_observed_at"], str):
            raise ConfigurationError("top1_observed_at debe ser fecha ISO con zona horaria.")
        from datetime import datetime
        try:
            if datetime.fromisoformat(expo["top1_observed_at"]).tzinfo is None:
                raise ValueError("Fecha sin zona horaria")
        except ValueError as error:
            raise ConfigurationError("Fecha del top 1 inválida.") from error
    number(expo["top1_max_age_seconds"], "top1_max_age_seconds", 1)
    if expo["economy_speed"] is not None:
        number(expo["economy_speed"], "economy_speed", 1)
    if expo["player_class"] not in {"discoverer", "collector", "general"}:
        raise ConfigurationError("Clase de jugador inválida.")
    if expo["cargo_ship"] not in {"small_cargo", "large_cargo"} or transport["cargo_ship"] not in {"small_cargo", "large_cargo"}:
        raise ConfigurationError("El carguero debe ser small_cargo o large_cargo.")
    if expo["combat_ship"] not in {None, "battleship", "destroyer", "reaper"}:
        raise ConfigurationError("Nave de acompañamiento no admitida.")
    boolean(expo["pathfinder"], "pathfinder")
    boolean(expo["probe"], "probe")
    number(expo["resource_bonus_percent"], "resource_bonus_percent", 0)
    if not 1 <= integer(expo["holding_hours"], "holding_hours", 1) <= 16:
        raise ConfigurationError("holding_hours debe estar entre 1 y 16.")
    integer(expo["max_parallel"], "max_parallel", 1)
    if transport["destination_id"] is not None:
        integer(transport["destination_id"], "destination_id", 1)
    if not 1 <= number(transport["minimum_utilization_percent"], "minimum_utilization_percent", 1) <= 100:
        raise ConfigurationError("Utilización de transporte fuera de rango.")
    leave = transport["leave_resources"]
    if not isinstance(leave, dict) or set(leave) != {"metal", "crystal", "deuterium"}:
        raise ConfigurationError("leave_resources debe definir los tres recursos.")
    for name, amount in leave.items():
        integer(amount, name)
    if not 0 <= integer(recycling["radius_systems"], "radius_systems") <= 20:
        raise ConfigurationError("El radio máximo implementado es 20 sistemas.")
    integer(recycling["minimum_debris"], "minimum_debris", 1)
    if not 1 <= integer(recycling["max_systems_per_cycle"], "max_systems_per_cycle", 1) <= 100:
        raise ConfigurationError("max_systems_per_cycle debe estar entre 1 y 100.")
    capacities = raw["capacities"]
    if not isinstance(capacities, dict) or any(name not in SHIP_IDS for name in capacities):
        raise ConfigurationError("Capacidades con naves desconocidas.")
    for name, amount in capacities.items():
        integer(amount, name, 0)
    selectors = raw["selectors"]
    if not isinstance(selectors, dict) or set(selectors) != SELECTOR_KEYS or any(not isinstance(value, str) or not value.strip() for value in selectors.values()):
        raise ConfigurationError("Contrato de selectores de flota inválido.")
    if "{id}" not in selectors["ship_input"] or "{mission}" not in selectors["mission"] or "{resource}" not in selectors["resource_input"]:
        raise ConfigurationError("Los selectores de nave, misión y recurso necesitan su parámetro.")
    minimum = number(raw["min_interval_seconds"], "min_interval_seconds", 1)
    maximum = number(raw["max_interval_seconds"], "max_interval_seconds", 1)
    if minimum > maximum:
        raise ConfigurationError("Intervalos de flota invertidos.")
    speed = integer(raw["speed_percent"], "speed_percent", 10)
    if speed not in range(10, 101, 10):
        raise ConfigurationError("speed_percent debe ser múltiplo de 10 entre 10 y 100.")
    if not isinstance(raw["plan_file"], str) or not raw["plan_file"].strip():
        raise ConfigurationError("Ruta del plan de flota inválida.")
    return FleetSettings(boolean(raw["enabled"], "enabled"), (base / raw["plan_file"]).resolve(), minimum, maximum,
        integer(raw["reserved_slots"], "reserved_slots"), integer(raw["deuterium_reserve"], "deuterium_reserve"),
        integer(raw["maximum_fuel"], "maximum_fuel", 1), integer(raw["maximum_oneway_seconds"], "maximum_oneway_seconds", 1),
        speed, dict(capacities), expo, transport, recycling, dict(selectors))
