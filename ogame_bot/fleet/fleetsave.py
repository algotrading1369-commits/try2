"""One-shot scheduled deployments, without inventing an incoming attack."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..domain.models import AccountSnapshot
from .models import Flight, SHIP_IDS
from .settings import FleetSettings


def plan_fleetsave(snapshot: AccountSnapshot, settings: FleetSettings, weights: dict[str, float], max_age: float) -> tuple[Flight | None, str]:
    plan = settings.fleetsave
    if not plan["enabled"]:
        return None, "fleetsave_disabled"
    slots = snapshot.fleet_slots.data or {}
    if not snapshot.movements.is_fresh(max_age) or (snapshot.movements.data or {}).get("threat_status") != "clear" or not snapshot.fleet_slots.is_fresh(max_age) or any(type(slots.get(key)) is not int for key in ("fleet_total","fleet_used")) or slots["fleet_total"]-slots["fleet_used"] <= 0:
        return None, "fleetsave_threats_or_slots_unknown"
    if not snapshot.outgoing.is_fresh(max_age) or snapshot.outgoing.status != "complete":
        return None, "fleetsave_movements_unknown"
    if plan["run_at"] and datetime.now(timezone.utc) < datetime.fromisoformat(plan["run_at"]):
        return None, "fleetsave_waiting_schedule"
    if not snapshot.registry.is_fresh(max_age):
        return None, "fleetsave_registry_unknown"
    origin = next((body for body in snapshot.bodies if body.body.id == plan["origin_id"]),None)
    destination = next((body for body in snapshot.registry.data or [] if body.id == plan["destination_id"] and body.id != plan["origin_id"]),None)
    if origin is None or destination is None:
        return None, "fleetsave_own_destination_unknown"
    if not origin.ships.is_fresh(max_age) or origin.ships.status != "complete" or not origin.resources.is_fresh(max_age):
        return None, "fleetsave_inventory_unknown"
    inventory = origin.ships.data or {}
    if any(type(inventory.get(ship)) is not int or inventory[ship] < 0 for ship in SHIP_IDS):
        return None, "fleetsave_inventory_unknown"
    ships = {ship:inventory[ship] for ship in SHIP_IDS if inventory[ship] > 0}
    if not ships or any(ship not in settings.capacities for ship in ships):
        return None, "fleetsave_ships_or_capacity_unknown"
    values = origin.resources.data or {}
    if any(type(values.get(key)) is not int or values[key] < 0 for key in ("metal","crystal","deuterium")) or values["deuterium"] <= 0:
        return None, "fleetsave_resources_unknown"
    capacity = sum(settings.capacities[ship]*amount for ship,amount in ships.items())
    load: dict[str,int] = {key:0 for key in ("metal","crystal","deuterium")}
    for key in sorted(load,key=lambda key:weights[key],reverse=True):
        load[key] = min(capacity,values[key])
        capacity -= load[key]
    action = Flight("fleet",4,origin.body.id,origin.body.coordinates,destination.coordinates,3 if destination.kind == "moon" else 1,destination.id,ships,load,plan["speed_percent"],1,"scheduled_own_deployment",purpose="fleetsave",origin_type=3 if origin.body.kind == "moon" else 1,minimum_duration_seconds=plan["minimum_flight_seconds"],plan_id=plan["plan_id"])
    action.validate()
    return action, "fleetsave_planned"
