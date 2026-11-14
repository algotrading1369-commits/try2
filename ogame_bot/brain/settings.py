"""Configuración validada del módulo 3, separada del navegador."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError
from .catalog import BUILD, RESEARCH, MINES


@dataclass(frozen=True)
class Goal:
    item: str
    target_level: int


@dataclass(frozen=True)
class BrainSettings:
    dry_run: bool
    journal_file: Path
    plan_file: Path
    min_interval_seconds: float
    max_interval_seconds: float
    builder_enabled: bool
    research_enabled: bool
    strategy: str
    build_goals: tuple[Goal, ...]
    research_goals: tuple[Goal, ...]
    reserve: dict[str, int]
    minimum_energy: int
    auto_solar: bool
    max_solar_level: int
    max_payback_days: float
    economy_speed: float | None
    resource_weights: dict[str, float]
    production_multipliers: dict[str, float]
    temperatures: dict[int, float]
    probe_target: int = 0
    probe_body_id: int | None = None
    probe_bootstrap_live: bool = False
    economic_live: bool = False


def effective_brain_settings(settings: BrainSettings, *, probe_completed: bool) -> BrainSettings:
    """A confirmed one-off bootstrap yields to the configured economic goals."""
    return replace(settings, probe_target=0, probe_bootstrap_live=False) if settings.probe_target and probe_completed else settings


def load_brain_settings(raw: dict[str, Any], base: Path) -> BrainSettings:
    expected = {
        "automation": {"dry_run", "journal_file", "plan_file", "min_interval_seconds", "max_interval_seconds"},
        "builder": {"enabled", "strategy", "priorities", "reserve", "minimum_energy", "auto_solar", "max_solar_level", "max_payback_days", "economy_speed", "resource_weights", "production_multipliers", "temperatures"},
        "research": {"enabled", "priorities"},
    }
    for section, keys in expected.items():
        optional = {"probe_target", "probe_body_id", "probe_bootstrap_live", "economic_live"} if section == "automation" else set()
        if not isinstance(raw.get(section), dict) or not keys <= set(raw[section]) or set(raw[section]) - keys - optional:
            raise ConfigurationError(f"Campos incorrectos en {section}.")

    def get(section: str, name: str, kind: type) -> Any:
        value = raw[section][name]
        if type(value) is not kind:
            raise ConfigurationError(f"Tipo inválido: {section}.{name}.")
        return value

    def number(value: Any, name: str, minimum: float = 0) -> float:
        if type(value) not in (float, int) or not math.isfinite(value) or value < minimum:
            raise ConfigurationError(f"Número inválido: {name}.")
        return float(value)

    def goals(section: str, catalog: dict[str, Any]) -> tuple[Goal, ...]:
        result: list[Goal] = []
        for row in get(section, "priorities", list):
            if not isinstance(row, dict) or set(row) != {"item", "target_level"} or not isinstance(row["item"], str) or row["item"] not in catalog or type(row["target_level"]) is not int or not 1 <= row["target_level"] <= 100:
                raise ConfigurationError(f"Objetivo inválido en {section}. Solo se admiten elementos del catálogo y niveles 1–100.")
            if any(goal.item == row["item"] for goal in result):
                raise ConfigurationError(f"Objetivo duplicado en {section}.")
            result.append(Goal(row["item"], row["target_level"]))
        return tuple(result)

    def mapping(name: str, *, integer: bool = False) -> dict[str, Any]:
        values = get("builder", name, dict)
        if set(values) != {"metal", "crystal", "deuterium"}:
            raise ConfigurationError(f"{name} debe definir metal, crystal y deuterium.")
        for resource, value in values.items():
            if integer and type(value) is not int:
                raise ConfigurationError("Las reservas deben ser enteros.")
            number(value, name + "." + resource, 0 if integer else 0.000001)
        return dict(values)

    strategy = get("builder", "strategy", str)
    if strategy not in {"priority", "roi"}:
        raise ConfigurationError("builder.strategy debe ser priority o roi.")
    build_goals = goals("builder", BUILD)
    if strategy == "roi" and any(goal.item not in MINES for goal in build_goals):
        raise ConfigurationError("La estrategia roi admite como objetivos las tres minas.")
    speed = raw["builder"]["economy_speed"]
    if speed is not None:
        speed = number(speed, "economy_speed", 1)
    if strategy == "roi" and speed is None:
        raise ConfigurationError("La estrategia roi requiere economy_speed explícita.")
    temperatures: dict[int, float] = {}
    for key, value in get("builder", "temperatures", dict).items():
        if type(key) is not int or key <= 0:
            raise ConfigurationError("temperatures usa identificadores numéricos de planeta.")
        if type(value) not in (int, float) or not math.isfinite(value) or not -300 <= value <= 359:
            raise ConfigurationError("Temperatura máxima inválida.")
        temperatures[key] = float(value)
    minimum_energy = get("builder", "minimum_energy", int)
    max_solar = get("builder", "max_solar_level", int)
    if minimum_energy < 0 or not 1 <= max_solar <= 100:
        raise ConfigurationError("minimum_energy debe ser no negativa y max_solar_level debe estar entre 1 y 100.")
    minimum_interval = number(raw["automation"]["min_interval_seconds"], "min_interval_seconds", 1)
    maximum_interval = number(raw["automation"]["max_interval_seconds"], "max_interval_seconds", 1)
    if minimum_interval > maximum_interval:
        raise ConfigurationError("El intervalo mínimo de automatización supera el máximo.")

    def path(name: str) -> Path:
        value = get("automation", name, str)
        if not value.strip():
            raise ConfigurationError("Ruta de automatización vacía.")
        return (base / value).resolve()

    probe_target = raw["automation"].get("probe_target", 0)
    probe_body = raw["automation"].get("probe_body_id")
    probe_live = raw["automation"].get("probe_bootstrap_live", False)
    economic_live = raw["automation"].get("economic_live", False)
    if type(economic_live) is not bool:
        raise ConfigurationError("economic_live debe ser un booleano.")
    if type(probe_target) is not int or probe_target not in {0, 1} or type(probe_live) is not bool:
        raise ConfigurationError("probe_target debe ser 0 o 1 y probe_bootstrap_live un booleano.")
    if probe_body is not None and (type(probe_body) is not int or probe_body <= 0):
        raise ConfigurationError("probe_body_id debe ser un identificador positivo.")
    if (probe_target == 1 and probe_body is None) or (probe_live and probe_target != 1):
        raise ConfigurationError("La preparación de sonda requiere objetivo 1 y planeta explícito.")
    return BrainSettings(
        dry_run=get("automation", "dry_run", bool), journal_file=path("journal_file"), plan_file=path("plan_file"),
        min_interval_seconds=minimum_interval, max_interval_seconds=maximum_interval,
        builder_enabled=get("builder", "enabled", bool), research_enabled=get("research", "enabled", bool),
        strategy=strategy, build_goals=build_goals, research_goals=goals("research", RESEARCH),
        reserve=mapping("reserve", integer=True), minimum_energy=minimum_energy,
        auto_solar=get("builder", "auto_solar", bool), max_solar_level=max_solar,
        max_payback_days=number(raw["builder"]["max_payback_days"], "max_payback_days", 0.01),
        economy_speed=speed, resource_weights=mapping("resource_weights"),
        production_multipliers=mapping("production_multipliers"), temperatures=temperatures,
        probe_target=probe_target, probe_body_id=probe_body, probe_bootstrap_live=probe_live, economic_live=economic_live,
    )
