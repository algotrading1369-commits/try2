"""Modelo clásico orientativo. Costes conservadores sin descuentos de formas de vida.

Fuentes históricas oficiales:
https://board.en.ogame.gameforge.com/index.php?thread/548906-formula-thread-v3/
https://board.en.ogame.gameforge.com/index.php?thread/78613-all-buildings-researches-fleets-and-defences/
No se modelan edificios de formas de vida, terraformers ni Gravitón.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext


@dataclass(frozen=True)
class Technology:
    id: int
    name_es: str
    component: str
    base: tuple[int, int, int]
    factor: str
    round_up_hundreds: bool = False

    def cost(self, target_level: int) -> dict[str, int]:
        if not 1 <= target_level <= 100:
            raise ValueError("Nivel fuera del intervalo soportado (1–100).")
        result: dict[str, int] = {}
        with localcontext() as context:
            context.prec = 150
            multiplier = Decimal(self.factor) ** (target_level - 1)
            for resource, amount in zip(("metal", "crystal", "deuterium"), self.base):
                value = Decimal(amount) * multiplier
                result[resource] = int((value / 100).to_integral_value(rounding=ROUND_CEILING)) * 100 if self.round_up_hundreds else int(value.to_integral_value(rounding=ROUND_FLOOR))
        return result


BUILD = {
    "metal_mine": Technology(1, "Mina de metal", "supplies", (60, 15, 0), "1.5"),
    "crystal_mine": Technology(2, "Mina de cristal", "supplies", (48, 24, 0), "1.6"),
    "deuterium_synthesizer": Technology(3, "Sintetizador de deuterio", "supplies", (225, 75, 0), "1.5"),
    "solar_plant": Technology(4, "Planta de energía solar", "supplies", (75, 30, 0), "1.5"),
    "metal_storage": Technology(22, "Almacén de metal", "supplies", (1000, 0, 0), "2"),
    "crystal_storage": Technology(23, "Almacén de cristal", "supplies", (1000, 500, 0), "2"),
    "deuterium_tank": Technology(24, "Contenedor de deuterio", "supplies", (1000, 1000, 0), "2"),
    "robotics_factory": Technology(14, "Fábrica de robots", "facilities", (400, 120, 200), "2"),
    "nanite_factory": Technology(15, "Fábrica de nanobots", "facilities", (1000000, 500000, 100000), "2"),
    "shipyard": Technology(21, "Hangar", "facilities", (400, 200, 100), "2"),
    "research_lab": Technology(31, "Laboratorio de investigación", "facilities", (200, 400, 200), "2"),
    "missile_silo": Technology(44, "Silo", "facilities", (20000, 20000, 1000), "2"),
}
RESEARCH = {
    "espionage_technology": Technology(106, "Tecnología de espionaje", "research", (200, 1000, 200), "2"),
    "computer_technology": Technology(108, "Tecnología de computación", "research", (0, 400, 600), "2"),
    "weapons_technology": Technology(109, "Tecnología militar", "research", (800, 200, 0), "2"),
    "shielding_technology": Technology(110, "Tecnología de defensa", "research", (200, 600, 0), "2"),
    "armour_technology": Technology(111, "Tecnología de blindaje", "research", (1000, 0, 0), "2"),
    "energy_technology": Technology(113, "Tecnología de energía", "research", (0, 800, 400), "2"),
    "hyperspace_technology": Technology(114, "Tecnología de hiperespacio", "research", (0, 4000, 2000), "2"),
    "combustion_drive": Technology(115, "Motor de combustión", "research", (400, 0, 600), "2"),
    "impulse_drive": Technology(117, "Motor de impulso", "research", (2000, 4000, 600), "2"),
    "hyperspace_drive": Technology(118, "Propulsor hiperespacial", "research", (10000, 20000, 6000), "2"),
    "laser_technology": Technology(120, "Tecnología láser", "research", (200, 100, 0), "2"),
    "ion_technology": Technology(121, "Tecnología iónica", "research", (1000, 300, 100), "2"),
    "plasma_technology": Technology(122, "Tecnología de plasma", "research", (2000, 4000, 1000), "2"),
    "intergalactic_research_network": Technology(123, "Red de investigación intergaláctica", "research", (240000, 400000, 160000), "2"),
    "astrophysics": Technology(124, "Astrofísica", "research", (4000, 8000, 4000), "1.75", True),
}
MINES = {"metal_mine": ("metal", 30, 10), "crystal_mine": ("crystal", 20, 10), "deuterium_synthesizer": ("deuterium", 10, 20)}


def level_curve(level: int) -> float:
    return float(Decimal(level) * Decimal("1.1") ** level)


def energy_delta(item: str, level: int) -> int:
    factor = MINES[item][2]
    # Ceil de la diferencia más una unidad: margen ante redondeos de niveles.
    return int((Decimal(factor) * (Decimal(level + 1) * Decimal("1.1") ** (level + 1) - Decimal(level) * Decimal("1.1") ** level)).to_integral_value(rounding=ROUND_CEILING)) + 1


def solar_gain(level: int) -> float:
    return 20 * (level_curve(level + 1) - level_curve(level))
