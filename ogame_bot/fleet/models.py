"""Órdenes de flota serializables y verificables."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

SHIP_IDS = {"small_cargo": 202, "large_cargo": 203, "espionage_probe": 210, "pathfinder": 219, "recycler": 209, "battleship": 207, "destroyer": 213, "reaper": 218}
SHIP_IDS.update({"light_fighter": 204, "heavy_fighter": 205, "cruiser": 206, "colony_ship": 208, "bomber": 211, "deathstar": 214, "battlecruiser": 215})
EXPO_POINTS = {"small_cargo": 20, "large_cargo": 60, "espionage_probe": 5, "pathfinder": 115, "battleship": 300, "destroyer": 550, "reaper": 700}


def coordinates(text: str) -> tuple[int, int, int]:
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+", text):
        raise ValueError("Coordenadas inválidas.")
    galaxy, system, position = (int(part) for part in text.split(":"))
    if not (1 <= galaxy <= 99 and 1 <= system <= 9999 and 1 <= position <= 16):
        raise ValueError("Coordenadas fuera del intervalo admitido.")
    return galaxy, system, position


@dataclass(frozen=True)
class Flight:
    kind: str
    mission: int
    origin_id: int
    origin_coordinates: str
    destination: str
    destination_type: int
    destination_id: int | None
    ships: dict[str, int]
    resources: dict[str, int]
    speed_percent: int
    holding_hours: int
    reason: str
    baseline_ids: tuple[str, ...] = ()
    purpose: str = "routine"
    threat_id: str | None = None
    impact_at: str | None = None
    origin_type: int = 1
    minimum_duration_seconds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if self.kind != "fleet" or type(self.mission) is not int or self.mission not in {3, 4, 6, 8, 15}:
            raise ValueError("Misión no admitida por el módulo 4.")
        if type(self.origin_id) is not int or self.origin_id <= 0:
            raise ValueError("Origen inválido.")
        if self.destination_id is not None and (type(self.destination_id) is not int or self.destination_id <= 0):
            raise ValueError("Destino propio inválido.")
        coordinates(self.origin_coordinates)
        _, _, position = coordinates(self.destination)
        if type(self.destination_type) is not int or self.destination_type not in {1, 2, 3}:
            raise ValueError("Tipo de destino inválido.")
        if self.mission == 15 and (position != 16 or self.destination_type != 1):
            raise ValueError("Las expediciones requieren posición 16.")
        if self.mission == 6 and (
            position != 16 or self.destination_type != 1 or self.destination_id is not None
            or coordinates(self.destination)[:2] != coordinates(self.origin_coordinates)[:2]
            or self.purpose != "defender" or self.speed_percent != 10
            or self.ships.get("espionage_probe", 0) < 1
            or not any(self.ships.get(ship, 0) > 0 for ship in ("small_cargo", "large_cargo"))
        ):
            raise ValueError("La evacuación de espionaje requiere carguera, sonda y posición 16 del mismo sistema al 10%.")
        if self.mission == 8 and (self.destination_type != 2 or self.ships.keys() != {"recycler"}):
            raise ValueError("Reciclaje requiere recicladores y destino escombros.")
        if self.mission in {3, 4} and (self.destination_id is None or self.destination_type not in {1, 3} or self.destination_id == self.origin_id):
            raise ValueError("El transporte requiere un destino propio distinto.")
        if not self.ships or any(key not in SHIP_IDS or type(value) is not int or value <= 0 for key, value in self.ships.items()):
            raise ValueError("Composición inválida.")
        if set(self.resources) != {"metal", "crystal", "deuterium"} or any(type(value) is not int or value < 0 for value in self.resources.values()):
            raise ValueError("Carga inválida.")
        if self.mission not in {3, 4, 6} and any(self.resources.values()):
            raise ValueError("La expedición o reciclaje no deben cargar recursos.")
        if type(self.speed_percent) is not int or self.speed_percent not in range(10, 101, 10):
            raise ValueError("Velocidad inválida.")
        if type(self.holding_hours) is not int or not 1 <= self.holding_hours <= 16:
            raise ValueError("Permanencia inválida.")
        if self.purpose not in {"routine", "defender"} or (self.mission == 4 and self.purpose != "defender"):
            raise ValueError("Despliegue reservado a evacuaciones verificadas.")
        if type(self.minimum_duration_seconds) is not int or self.minimum_duration_seconds < 0 or type(self.origin_type) is not int or self.origin_type not in {1, 3}:
            raise ValueError("Duración mínima o tipo de origen inválidos.")
        if self.purpose == "defender":
            from datetime import datetime
            if self.mission not in {3, 4, 6} or not self.threat_id or not self.impact_at or self.minimum_duration_seconds <= 0:
                raise ValueError("Faltan datos de la amenaza defensiva.")
            if datetime.fromisoformat(self.impact_at).tzinfo is None:
                raise ValueError("La llegada del ataque debe incluir zona horaria.")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Flight:
        data = dict(raw)
        data["baseline_ids"] = tuple(data.get("baseline_ids", ()))
        flight = cls(**data)
        flight.validate()
        return flight
