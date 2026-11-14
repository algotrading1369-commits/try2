"""Configuración de defensa y notificaciones, sin secretos en YAML."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError


@dataclass(frozen=True)
class DefenderSettings:
    enabled: bool
    report_file: Path
    origins: tuple[int, ...]
    destinations: tuple[int, ...]
    mission: int
    speed_percent: int
    trigger_seconds: float
    minimum_lead_seconds: float
    max_state_age_seconds: float
    minimum_flight_seconds: int
    maximum_flight_seconds: int
    maximum_fuel: int
    resource_policy: str
    pause_on_unknown: bool
    idle_min_seconds: float
    idle_max_seconds: float
    alert_min_seconds: float
    alert_max_seconds: float


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool
    token_env: str
    chat_id_env: str
    timeout_seconds: float
    cooldown_seconds: float
    state_file: Path


def load_defender_settings(raw: Any, telegram: Any, base: Path) -> tuple[DefenderSettings, TelegramSettings]:
    keys = {"enabled", "report_file", "origins", "destinations", "mission", "speed_percent", "trigger_seconds", "minimum_lead_seconds", "max_state_age_seconds", "minimum_flight_seconds", "maximum_flight_seconds", "maximum_fuel", "resource_policy", "pause_on_unknown", "idle_min_seconds", "idle_max_seconds", "alert_min_seconds", "alert_max_seconds"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ConfigurationError("Campos incorrectos en defender.")
    if not isinstance(telegram, dict) or set(telegram) != {"enabled", "token_env", "chat_id_env", "timeout_seconds", "cooldown_seconds", "state_file"}:
        raise ConfigurationError("Campos incorrectos en telegram.")

    def boolean(value: Any) -> bool:
        if type(value) is not bool:
            raise ConfigurationError("Se requiere un booleano en defensa/Telegram.")
        return value

    def number(value: Any, minimum: float = 1, *, integer: bool = False) -> Any:
        if (type(value) is not int if integer else type(value) not in (int, float)) or not math.isfinite(value) or value < minimum:
            raise ConfigurationError("Número fuera de rango en defensa/Telegram.")
        return value

    def ids(key: str) -> tuple[int, ...]:
        value = raw[key]
        if not isinstance(value, list) or any(type(item) is not int or item <= 0 for item in value) or len(set(value)) != len(value):
            raise ConfigurationError("Los orígenes y destinos defensivos requieren IDs propios únicos.")
        return tuple(value)

    def path(value: Any) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError("Ruta vacía en defensa/Telegram.")
        return (base / value).resolve()

    if raw["mission"] not in {"deployment", "transport", "espionage16"} or raw["resource_policy"] not in {"maximize_value", "require_all"}:
        raise ConfigurationError("Misión o política de recursos defensiva inválida.")
    speed = number(raw["speed_percent"], integer=True)
    if speed not in range(10, 101, 10):
        raise ConfigurationError("La velocidad defensiva debe ser 10–100 en incrementos de 10.")
    if raw["mission"] == "espionage16" and speed != 10:
        raise ConfigurationError("La evacuación espionage16 requiere velocidad 10%.")
    for lower, upper in (("idle_min_seconds", "idle_max_seconds"), ("alert_min_seconds", "alert_max_seconds"), ("minimum_flight_seconds", "maximum_flight_seconds")):
        if number(raw[lower]) > number(raw[upper]):
            raise ConfigurationError("Intervalos defensivos invertidos.")
    if number(raw["minimum_lead_seconds"]) >= number(raw["trigger_seconds"]):
        raise ConfigurationError("El margen mínimo debe ser menor que el umbral de evacuación.")
    if number(raw["max_state_age_seconds"]) >= raw["trigger_seconds"]:
        raise ConfigurationError("La vigencia defensiva debe ser menor que el umbral de evacuación.")
    for name in ("token_env", "chat_id_env"):
        if not isinstance(telegram[name], str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", telegram[name]):
            raise ConfigurationError("Telegram requiere nombres de variables de entorno, no credenciales.")
    return DefenderSettings(
        boolean(raw["enabled"]), path(raw["report_file"]), ids("origins"), ids("destinations"),
        {"deployment": 4, "transport": 3, "espionage16": 6}[raw["mission"]], speed,
        number(raw["trigger_seconds"]), number(raw["minimum_lead_seconds"]), number(raw["max_state_age_seconds"]),
        number(raw["minimum_flight_seconds"], integer=True), number(raw["maximum_flight_seconds"], integer=True), number(raw["maximum_fuel"], integer=True),
        raw["resource_policy"], boolean(raw["pause_on_unknown"]),
        number(raw["idle_min_seconds"]), number(raw["idle_max_seconds"]), number(raw["alert_min_seconds"]), number(raw["alert_max_seconds"]),
    ), TelegramSettings(boolean(telegram["enabled"]), telegram["token_env"], telegram["chat_id_env"], number(telegram["timeout_seconds"]), number(telegram["cooldown_seconds"]), path(telegram["state_file"]))
