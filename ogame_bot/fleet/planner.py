"""Planes sin navegador; no presenta los modelos históricos como reglas verificadas."""
from __future__ import annotations

import math
from typing import Any

from ..domain.models import AccountSnapshot, BodySnapshot
from .models import EXPO_POINTS, Flight, coordinates
from .settings import FleetSettings


def base_cap(points: int) -> int | None:
    """Tabla histórica de recursos; puntos inferiores a 10.000 necesitan override."""
    if points < 10000:
        return None
    for boundary, cap in ((100000, 500000), (1000000, 1200000), (5000000, 1800000), (25000000, 2400000), (50000000, 3000000), (75000000, 3600000), (100000000, 4200000)):
        if points < boundary:
            return cap
    return 5000000


def expedition_composition(settings: FleetSettings, points: int) -> tuple[dict[str, int], int]:
    expo = settings.expeditions
    cap = expo["base_cap_override"] or base_cap(points)
    if cap is None or expo["economy_speed"] is None:
        raise ValueError("Faltan el modelo del top 1 o la velocidad económica.")
    multiplier = (1.5 * expo["economy_speed"] if expo["player_class"] == "discoverer" else 1) * (2 if expo["pathfinder"] else 1) * (1 + expo["resource_bonus_percent"] / 100)
    target_cargo = math.ceil(cap * multiplier) + settings.maximum_fuel
    ships: dict[str, int] = {}
    if expo["pathfinder"]:
        ships["pathfinder"] = 1
    if expo["probe"]:
        ships["espionage_probe"] = 1
    if expo["combat_ship"]:
        ships[expo["combat_ship"]] = 1
    cargo_ship = expo["cargo_ship"]
    capacity = settings.capacities.get(cargo_ship, 0)
    if capacity <= 0:
        raise ValueError("Falta la capacidad efectiva del carguero.")
    helper_capacity = sum(settings.capacities.get(ship, 0) * count for ship, count in ships.items())
    helper_points = sum(EXPO_POINTS[ship] * count for ship, count in ships.items())
    ships[cargo_ship] = max(1, math.ceil(max(0, target_cargo - helper_capacity) / capacity), math.ceil(max(0, cap / 200 - helper_points) / EXPO_POINTS[cargo_ship]))
    return ships, target_cargo


class FleetPlanner:
    def __init__(self, settings: FleetSettings, max_age: float) -> None:
        self.settings = settings
        self.max_age = max_age
        self.notes: list[dict[str, Any]] = []

    def _body(self, snapshot: AccountSnapshot, identifier: int) -> BodySnapshot | None:
        return next((body for body in snapshot.bodies if body.body.id == identifier), None)

    def _available(self, body: BodySnapshot, ships: dict[str, int], *, quoted_later: bool = False) -> bool:
        fuel = 1 if quoted_later else self.settings.maximum_fuel
        return body.ships.is_fresh(self.max_age) and body.resources.is_fresh(self.max_age) and all(type((body.ships.data or {}).get(ship)) is int and body.ships.data[ship] >= count for ship, count in ships.items()) and (body.resources.data or {}).get("deuterium", -1) >= self.settings.deuterium_reserve + fuel

    def plan(self, snapshot: AccountSnapshot) -> list[Flight]:
        self.notes = []
        settings = self.settings
        if not settings.enabled:
            return []
        if not snapshot.movements.is_fresh(self.max_age) or (snapshot.movements.data or {}).get("threat_status") != "clear":
            self.notes.append({"reason": "attacks_unknown_detected_or_expired"})
            return []
        if not snapshot.fleet_slots.is_fresh(self.max_age) or snapshot.fleet_slots.status != "complete" or not snapshot.outgoing.is_fresh(self.max_age) or snapshot.outgoing.status != "complete":
            self.notes.append({"reason": "slots_or_outgoing_unverified"})
            return []
        slots = snapshot.fleet_slots.data or {}
        if slots["fleet_total"] - slots["fleet_used"] <= settings.reserved_slots:
            self.notes.append({"reason": "reserved_fleet_slots"})
            return []
        baseline = tuple(flight["id"] for flight in snapshot.outgoing.data or [])
        zero = {"metal": 0, "crystal": 0, "deuterium": 0}
        actions: list[Flight] = []
        # Son alternativas: se ejecutará solo una y se releerá antes del siguiente envío.
        expo = settings.expeditions
        if expo["enabled"] and slots["expedition_total"] > slots["expedition_used"] and slots["expedition_used"] < expo["max_parallel"]:
            manual = expo.get("composition_mode", "automatic") == "manual"
            if manual or (snapshot.top1.is_fresh(expo["top1_max_age_seconds"]) and snapshot.top1.data):
                try:
                    ships, target = (dict(expo["manual_ships"]), None) if manual else expedition_composition(settings, snapshot.top1.data["points"])
                    for identifier in expo["origins"]:
                        body = self._body(snapshot, identifier)
                        if body is not None and self._available(body, ships, quoted_later=True):
                            galaxy, system, _ = coordinates(body.body.coordinates)
                            actions.append(Flight("fleet", 15, identifier, body.body.coordinates, f"{galaxy}:{system}:16", 1, None, ships, dict(zero), settings.speed_percent, expo["holding_hours"], "manual_composition" if manual else f"modeled_resource_capacity_{target}", baseline))
                            break
                    else:
                        self.notes.append({"reason": "expedition_ships_or_fuel_unavailable"})
                except ValueError as error:
                    self.notes.append({"reason": "expedition_model_incomplete", "detail": str(error)})
            else:
                self.notes.append({"reason": "top1_unknown_or_expired"})
        elif expo["enabled"]:
            self.notes.append({"reason": "expedition_slots_exhausted"})
        transport = settings.transports
        if transport["enabled"]:
            destination = next((body for body in snapshot.registry.data or [] if body.id == transport["destination_id"]), None)
            capacity = settings.capacities.get(transport["cargo_ship"], 0)
            if destination is None or capacity <= 0 or not snapshot.registry.is_fresh(self.max_age):
                self.notes.append({"reason": "own_transport_destination_or_capacity_unknown"})
            else:
                for identifier in transport["sources"]:
                    body = self._body(snapshot, identifier)
                    if body is None or identifier == destination.id or not body.ships.is_fresh(self.max_age) or not body.resources.is_fresh(self.max_age):
                        continue
                    count = (body.ships.data or {}).get(transport["cargo_ship"], 0)
                    values = body.resources.data or {}
                    if count <= 0 or not all(type(values.get(key)) is int for key in zero):
                        continue
                    available = {key: max(0, values[key] - max(transport["leave_resources"][key], settings.deuterium_reserve if key == "deuterium" else 0)) for key in zero}
                    load_total = min(sum(available.values()), count * capacity)
                    if load_total <= 0:
                        continue
                    count = min(count, math.ceil(load_total / capacity))
                    load: dict[str, int] = {}
                    remaining = load_total
                    for key in zero:
                        load[key] = min(available[key], remaining)
                        remaining -= load[key]
                    if sum(load.values()) * 100 < count * capacity * transport["minimum_utilization_percent"] or not self._available(body, {transport["cargo_ship"]: count}, quoted_later=True):
                        continue
                    actions.append(Flight("fleet", 3, identifier, body.body.coordinates, destination.coordinates, 3 if destination.kind == "moon" else 1, destination.id, {transport["cargo_ship"]: count}, load, settings.speed_percent, 1, "own_resource_transport", baseline))
        recycling = settings.recycling
        if recycling["enabled"] and snapshot.debris.is_fresh(self.max_age):
            capacity = settings.capacities.get("recycler", 0)
            for identifier in recycling["origins"]:
                body = self._body(snapshot, identifier)
                if body is None or capacity <= 0:
                    continue
                galaxy, system, _ = coordinates(body.body.coordinates)
                for field in sorted(snapshot.debris.data or [], key=lambda item: item["metal"] + item["crystal"], reverse=True):
                    target_galaxy, target_system, _ = coordinates(field["coordinates"])
                    amount = field["metal"] + field["crystal"]
                    if target_galaxy != galaxy or abs(target_system - system) > recycling["radius_systems"] or amount < recycling["minimum_debris"]:
                        continue
                    ships = {"recycler": math.ceil(amount / capacity)}
                    if self._available(body, ships, quoted_later=True):
                        actions.append(Flight("fleet", 8, identifier, body.body.coordinates, field["coordinates"], 2, None, ships, dict(zero), settings.speed_percent, 1, "nearby_debris", baseline))
                        break
        for action in actions:
            action.validate()
        return actions
