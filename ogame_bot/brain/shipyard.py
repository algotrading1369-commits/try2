"""Finite production goals backed by confirmed receipts, never by stock deficits."""
from __future__ import annotations

from typing import Any

from ..domain.models import AccountSnapshot
from ..storage.orders import OrderJournal
from .catalog import SHIP_BUILD
from .planner import Upgrade, free
from .settings import BrainSettings


def plan_shipyard(snapshot: AccountSnapshot, settings: BrainSettings, journal: OrderJournal, max_age: float) -> tuple[list[Upgrade], list[dict[str, Any]]]:
    actions: list[Upgrade] = []
    notes: list[dict[str, Any]] = []
    if not settings.shipyard_enabled or settings.probe_target:
        return actions, notes
    if not snapshot.movements.is_fresh(max_age) or (snapshot.movements.data or {}).get("threat_status") != "clear":
        return actions, [{"reason": "shipyard_attacks_unknown_or_detected"}]
    visited: set[int] = set()
    for goal in settings.shipyard_queue:
        produced = journal.produced(goal.id, goal.body_id, goal.item)
        if produced >= goal.quantity:
            continue
        if goal.body_id in visited:
            continue
        visited.add(goal.body_id)
        body = next((body for body in snapshot.bodies if body.body.id == goal.body_id and body.body.kind == "planet"), None)
        reason: str | None = None
        if body is None:
            reason = "shipyard_origin_unknown"
        elif not free(body.shipyard_queue, max_age):
            reason = "shipyard_queue_busy_unknown_or_expired"
        elif not body.ships.is_fresh(max_age) or not body.resources.is_fresh(max_age):
            reason = "shipyard_inventory_or_resources_expired"
        else:
            current = (body.ships.data or {}).get(goal.item)
            if type(current) is not int or current < 0:
                reason = "shipyard_inventory_unknown"
            else:
                unit = SHIP_BUILD[goal.item].cost(1)
                values = body.resources.data or {}
                if any(type(values.get(key)) is not int for key in unit):
                    reason = "shipyard_resources_unknown"
                else:
                    batch = min(goal.quantity-produced, settings.shipyard_max_batch, *(max(0, values[key]-settings.reserve[key])//amount for key,amount in unit.items() if amount > 0))
                    if batch < 1:
                        reason = "shipyard_resources_or_reserve_insufficient"
                    else:
                        actions.append(Upgrade("shipyard", goal.item, goal.body_id, body.body.coordinates, current, current+batch, {key:amount*batch for key,amount in unit.items()}, "production_queue", production_key=goal.id))
        if reason:
            notes.append({"body_id":goal.body_id,"production_key":goal.id,"reason":reason,"produced":produced,"target":goal.quantity})
    return actions, notes
