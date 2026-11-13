"""Planificador puro: prioridades estrictas o ROI estimado con energía incluida."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..domain.models import AccountSnapshot, BodySnapshot, Reading
from .catalog import BUILD, RESEARCH, MINES, Technology, energy_delta, level_curve, solar_gain
from .settings import BrainSettings, Goal


@dataclass(frozen=True)
class Upgrade:
    kind: str
    item: str
    body_id: int
    coordinates: str
    current_level: int
    target_level: int
    cost: dict[str, int]
    reason: str
    payback_days: float | None = None

    @property
    def technology(self) -> Technology:
        return (BUILD if self.kind == "building" else RESEARCH)[self.item]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Upgrade:
        item = cls(**raw)
        if item.kind not in {"building", "research"} or item.item not in (BUILD if item.kind == "building" else RESEARCH):
            raise ValueError("Orden con tecnología desconocida.")
        if type(item.body_id) is not int or item.body_id <= 0 or type(item.current_level) is not int or type(item.target_level) is not int or not 0 <= item.current_level < 100 or item.target_level != item.current_level + 1:
            raise ValueError("Orden con cuerpo o nivel inválido.")
        if item.cost != item.technology.cost(item.target_level):
            raise ValueError("El coste de la orden no coincide con el modelo.")
        return item


def level(reading: Reading[dict[str, int]], item: str, max_age: float) -> int | None:
    if not reading.is_fresh(max_age):
        return None
    value = (reading.data or {}).get(item)
    return value if type(value) is int and 0 <= value <= 100 else None


def free(reading: Reading[dict[str, str]], max_age: float) -> bool:
    return reading.is_fresh(max_age) and (reading.data or {}).get("state") == "free"


def afford(resources: dict[str, int], cost: dict[str, int], reserve: dict[str, int]) -> bool:
    return all(type(resources.get(resource)) is int and resources[resource] >= amount + reserve[resource] for resource, amount in cost.items())


class Planner:
    def __init__(self, settings: BrainSettings, max_age: float) -> None:
        self.settings = settings
        self.max_age = max_age
        self.notes: list[dict[str, Any]] = []

    def note(self, body_id: int | None, reason: str) -> None:
        self.notes.append({"body_id": body_id, "reason": reason})

    def _resources(self, body: BodySnapshot) -> dict[str, int] | None:
        values = body.resources.data or {}
        if not body.resources.is_fresh(self.max_age) or not all(type(values.get(key)) is int for key in ("metal", "crystal", "deuterium", "energy")):
            self.note(body.body.id, "resources_unknown_or_expired")
            return None
        return values

    def _upgrade(self, body: BodySnapshot, item: str, current: int, reason: str, *, research: bool = False, payback: float | None = None) -> Upgrade:
        catalog = RESEARCH if research else BUILD
        return Upgrade("research" if research else "building", item, body.body.id, body.body.coordinates, current, current + 1, catalog[item].cost(current + 1), reason, payback)

    def _solar(self, body: BodySnapshot, reason: str) -> Upgrade | None:
        solar = level(body.buildings, "solar_plant", self.max_age)
        if not self.settings.auto_solar or solar is None or solar >= self.settings.max_solar_level:
            self.note(body.body.id, "solar_unavailable_or_at_limit")
            return None
        return self._upgrade(body, "solar_plant", solar, reason)

    def _goal_level(self, body: BodySnapshot, goal: Goal) -> int | None:
        return level(body.buildings if BUILD[goal.item].component == "supplies" else body.facilities, goal.item, self.max_age)

    def _priority(self, body: BodySnapshot, resources: dict[str, int]) -> Upgrade | None:
        for goal in self.settings.build_goals:
            current = self._goal_level(body, goal)
            if current is None:
                self.note(body.body.id, "priority_level_unknown_" + goal.item)
                return None
            if current >= goal.target_level:
                continue
            if goal.item in MINES and resources["energy"] - energy_delta(goal.item, current) < self.settings.minimum_energy:
                return self._solar(body, "energy_for_" + goal.item)
            return self._upgrade(body, goal.item, current, "priority")
        self.note(body.body.id, "building_goals_complete")
        return None

    def _roi(self, body: BodySnapshot, resources: dict[str, int]) -> Upgrade | None:
        settings = self.settings
        candidates: list[Upgrade] = []
        for goal in settings.build_goals:
            current = self._goal_level(body, goal)
            if current is None:
                # No se afirma que el óptimo esté probado si falta una mina configurada.
                self.note(body.body.id, "roi_level_unknown_" + goal.item)
                return None
            if current >= goal.target_level:
                continue
            resource, coefficient, _ = MINES[goal.item]
            temperature_factor = 1.0
            if resource == "deuterium":
                temperature = settings.temperatures.get(body.body.id)
                if temperature is None:
                    self.note(body.body.id, "roi_temperature_unknown")
                    return None
                temperature_factor = 1.44 - 0.004 * temperature
            if settings.economy_speed is None:
                self.note(body.body.id, "roi_economy_speed_unknown")
                return None
            hourly = coefficient * (level_curve(current + 1) - level_curve(current)) * settings.economy_speed * settings.production_multipliers[resource] * temperature_factor
            costs = dict(BUILD[goal.item].cost(current + 1))
            deficit = self.settings.minimum_energy + energy_delta(goal.item, current) - resources["energy"]
            needs_solar = deficit > 0
            solar = level(body.buildings, "solar_plant", self.max_age)
            if needs_solar:
                if not settings.auto_solar or solar is None:
                    self.note(body.body.id, "roi_solar_unknown")
                    continue
                next_solar = solar
                while deficit > 0 and next_solar < settings.max_solar_level:
                    deficit -= solar_gain(next_solar)
                    for key, amount in BUILD["solar_plant"].cost(next_solar + 1).items():
                        costs[key] += amount
                    next_solar += 1
                if deficit > 0:
                    self.note(body.body.id, "roi_solar_limit")
                    continue
            weighted_cost = sum(amount * settings.resource_weights[key] for key, amount in costs.items())
            payback = weighted_cost / (hourly * settings.resource_weights[resource]) / 24
            if payback > settings.max_payback_days:
                self.note(body.body.id, "roi_above_payback_limit_" + goal.item)
                continue
            if needs_solar:
                assert solar is not None
                candidates.append(self._upgrade(body, "solar_plant", solar, "energy_for_" + goal.item, payback=payback))
            else:
                candidates.append(self._upgrade(body, goal.item, current, "roi", payback=payback))
        return min(candidates, key=lambda candidate: candidate.payback_days or float("inf")) if candidates else None

    def plan(self, snapshot: AccountSnapshot) -> list[Upgrade]:
        self.notes = []
        if not snapshot.movements.is_fresh(self.max_age) or (snapshot.movements.data or {}).get("threat_status") != "clear":
            self.note(None, "attacks_detected_unknown_or_expired")
            return []
        proposals: list[Upgrade] = []
        for body in snapshot.bodies:
            if body.body.kind != "planet":
                continue
            resources = self._resources(body)
            if resources is None:
                continue
            if not free(body.construction_queue, self.max_age):
                self.note(body.body.id, "construction_queue_busy_unknown_or_expired")
                continue
            if self.settings.builder_enabled:
                if resources["energy"] < self.settings.minimum_energy:
                    proposal = self._solar(body, "energy_deficit")
                else:
                    proposal = self._priority(body, resources) if self.settings.strategy == "priority" else self._roi(body, resources)
                if proposal is not None:
                    if afford(resources, proposal.cost, self.settings.reserve):
                        proposals.append(proposal)
                    else:
                        self.note(body.body.id, "building_resources_or_reserve_insufficient")
        if self.settings.research_enabled:
            if not free(snapshot.research_queue, self.max_age):
                self.note(None, "research_queue_busy_unknown_or_expired")
            else:
                for goal in self.settings.research_goals:
                    current = level(snapshot.research, goal.item, self.max_age)
                    if current is None:
                        self.note(None, "research_level_unknown_" + goal.item)
                        break
                    if current >= goal.target_level:
                        continue
                    for body in snapshot.bodies:
                        if body.body.kind != "planet" or not free(body.construction_queue, self.max_age):
                            continue
                        resources = self._resources(body)
                        if resources is not None and afford(resources, RESEARCH[goal.item].cost(current + 1), self.settings.reserve):
                            proposals.append(self._upgrade(body, goal.item, current, "research_priority", research=True))
                            break
                    else:
                        self.note(None, "research_resources_or_reserve_insufficient")
                    break
        return proposals
