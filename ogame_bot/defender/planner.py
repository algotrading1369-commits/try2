"""Plan de evacuación: todas las naves móviles confirmadas y carga limitada."""
from __future__ import annotations

import math

from ..domain.models import AccountSnapshot, CelestialBody
from ..fleet.models import Flight, SHIP_IDS
from .settings import DefenderSettings
from .threats import Threat, resolve_target


def destination_options(snapshot: AccountSnapshot, origin_id: int, settings: DefenderSettings) -> tuple[list[CelestialBody], str | None]:
    """Verifica destinos propios sin inventarlos ni seleccionar el origen."""
    if not snapshot.registry.is_fresh(settings.max_state_age_seconds):
        return [], "own_destinations_expired"
    bodies = snapshot.registry.data or []
    if not any(body.id != origin_id for body in bodies):
        return [], "no_other_own_body"
    if not settings.destinations:
        return [], "destinations_not_configured"
    if not snapshot.movements.is_fresh(settings.max_state_age_seconds):
        return [], "destination_threats_unknown_or_expired"
    threatened: set[int] = set()
    for event in (snapshot.movements.data or {}).get("events", []):
        if event.get("possible_incoming_attack"):
            target = resolve_target(event, bodies)
            if target is not None:
                threatened.add(target.id)
            elif event.get("destination"):
                threatened.update(body.id for body in bodies if body.coordinates == event["destination"])
    candidates = [body for identifier in settings.destinations for body in bodies
                  if body.id == identifier and body.id != origin_id and body.id not in threatened]
    return candidates, None if candidates else "no_verified_own_destination_outside_known_threats"


class EscapePlanner:
    def __init__(self, settings: DefenderSettings, capacities: dict[str, int], weights: dict[str, float]) -> None:
        self.settings, self.capacities, self.weights = settings, capacities, weights
        self.reason = "not_evaluated"

    def plan(self, snapshot: AccountSnapshot, threat: Threat) -> Flight | None:
        settings = self.settings
        self.reason = "threat_not_eligible"
        if not settings.enabled or not threat.eligible or threat.body_id is None:
            return None
        destinations, reason = destination_options(snapshot, threat.body_id, settings)
        if reason is not None:
            self.reason = reason
            return None
        body = next((body for body in snapshot.bodies if body.body.id == threat.body_id), None)
        if body is None or not body.ships.is_fresh(settings.max_state_age_seconds) or body.ships.status != "complete" or not body.resources.is_fresh(settings.max_state_age_seconds):
            self.reason = "inventory_unknown_or_expired"
            return None
        available = body.ships.data or {}
        if not all(type(available.get(ship)) is int and available[ship] >= 0 for ship in SHIP_IDS):
            self.reason = "incomplete_movable_ship_inventory"
            return None
        ships = {ship: count for ship, count in available.items() if count > 0 and ship in SHIP_IDS}
        if not ships:
            self.reason = "no_movable_ships"
            return None
        slot_data = snapshot.fleet_slots.data or {}
        if not snapshot.fleet_slots.is_fresh(settings.max_state_age_seconds) or not all(type(slot_data.get(key)) is int for key in ("fleet_total", "fleet_used")) or not 0 <= slot_data["fleet_used"] < slot_data["fleet_total"]:
            self.reason = "no_confirmed_free_fleet_slot"
            return None
        if any(ship not in self.capacities for ship in ships):
            self.reason = "effective_ship_capacity_unknown"
            return None
        values = body.resources.data or {}
        if not all(type(values.get(key)) is int and values[key] >= 0 for key in ("metal", "crystal", "deuterium")) or values["deuterium"] < settings.maximum_fuel:
            self.reason = "resources_or_fuel_unverified"
            return None
        capacity = sum(self.capacities[ship] * count for ship, count in ships.items()) - settings.maximum_fuel
        if capacity < 0:
            self.reason = "fuel_margin_exceeds_capacity"
            return None
        resources = {key: values[key] - (settings.maximum_fuel if key == "deuterium" else 0) for key in ("metal", "crystal", "deuterium")}
        if settings.resource_policy == "require_all" and sum(resources.values()) > capacity:
            self.reason = "cannot_carry_all_resources"
            return None
        load = {key: 0 for key in resources}
        for key in sorted(resources, key=lambda key: self.weights[key], reverse=True):
            load[key] = min(capacity, resources[key])
            capacity -= load[key]
        for destination in destinations:
            flight = Flight("fleet", settings.mission, body.body.id, body.body.coordinates, destination.coordinates,
                3 if destination.kind == "moon" else 1, destination.id, ships, load, settings.speed_percent, 1, "defensive_evacuation",
                purpose="defender", threat_id=threat.id, impact_at=threat.impact_at,
                origin_type=3 if body.body.kind == "moon" else 1,
                minimum_duration_seconds=max(settings.minimum_flight_seconds, math.ceil(threat.seconds_to_impact or 0) + math.ceil(settings.minimum_lead_seconds)))
            flight.validate()
            self.reason = "planned"
            return flight
        self.reason = "no_verified_own_destination_outside_known_threats"
        return None
