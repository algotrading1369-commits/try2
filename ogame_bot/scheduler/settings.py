"""Parámetros del scheduler; los intervalos de cada módulo conservan su configuración."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError


@dataclass(frozen=True)
class SchedulerSettings:
    status_file: Path
    heartbeat_seconds: float
    idle_poll_seconds: float
    retry_initial_seconds: float
    retry_max_seconds: float
    max_consecutive_failures: int
    dependency_retry_seconds: float
    reconciliation_min_seconds: float
    reconciliation_max_seconds: float
    priorities: dict[str, int]
    isolate_failures: bool = True


def load_scheduler_settings(raw: Any, base: Path) -> SchedulerSettings:
    keys = {"status_file", "heartbeat_seconds", "idle_poll_seconds", "retry_initial_seconds", "retry_max_seconds", "max_consecutive_failures", "dependency_retry_seconds", "reconciliation_min_seconds", "reconciliation_max_seconds", "priorities"}
    if not isinstance(raw, dict) or set(raw)-{"isolate_failures"} != keys:
        raise ConfigurationError("Campos incorrectos en scheduler.")
    isolate = raw.get("isolate_failures",True)
    if type(isolate) is not bool:
        raise ConfigurationError("scheduler.isolate_failures debe ser booleano.")

    def number(name: str) -> float:
        value = raw[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ConfigurationError("Valor numérico inválido en scheduler." + name)
        return float(value)

    if not isinstance(raw["status_file"], str) or not raw["status_file"].strip():
        raise ConfigurationError("scheduler.status_file debe ser una ruta no vacía.")
    count = raw["max_consecutive_failures"]
    if type(count) is not int or not 1 <= count <= 20:
        raise ConfigurationError("max_consecutive_failures debe estar entre 1 y 20.")
    priorities = raw["priorities"]
    if not isinstance(priorities, dict) or set(priorities) != {"defender", "session", "reconcile", "scanner", "brain", "fleet"} or any(type(value) is not int or value < 0 for value in priorities.values()):
        raise ConfigurationError("Mapa de prioridades del scheduler inválido.")
    if not priorities["defender"] < priorities["session"] < min(value for name, value in priorities.items() if name not in {"defender", "session"}):
        raise ConfigurationError("Defender y sesión deben tener las prioridades más altas, en ese orden.")
    if priorities["scanner"] >= min(priorities["brain"], priorities["fleet"]):
        raise ConfigurationError("El scanner debe preceder a Brain y flotas.")
    initial, maximum = number("retry_initial_seconds"), number("retry_max_seconds")
    minimum, maximum_reconcile = number("reconciliation_min_seconds"), number("reconciliation_max_seconds")
    if initial > maximum or minimum > maximum_reconcile:
        raise ConfigurationError("Intervalos del scheduler invertidos.")
    return SchedulerSettings((base / raw["status_file"]).resolve(), number("heartbeat_seconds"), number("idle_poll_seconds"), initial, maximum, count, number("dependency_retry_seconds"), minimum, maximum_reconcile, dict(priorities),isolate)
