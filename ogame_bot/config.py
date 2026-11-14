"""Carga YAML con validación estricta y rutas relativas al archivo."""
from __future__ import annotations

import math
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .brain.settings import BrainSettings, load_brain_settings
from .fleet.settings import FleetSettings, load_fleet_settings
from .defender.settings import DefenderSettings, TelegramSettings, load_defender_settings
from .scheduler.settings import SchedulerSettings, load_scheduler_settings


@dataclass(frozen=True)
class Config:
    lobby_url: str
    community: str
    server_number: int
    expected_player_name: str
    headless: bool
    channel: str | None
    profile_dir: Path
    locale: str
    navigation_timeout_seconds: float
    action_timeout_seconds: float
    manual_login_timeout_seconds: float
    health_check_seconds: float
    max_reconnect_attempts: int
    initial_backoff_seconds: float
    max_backoff_seconds: float
    min_delay_seconds: float
    max_delay_seconds: float
    log_file: Path
    rotation_mb: int
    backup_count: int
    scanner_enabled: bool
    scanner_output_file: Path
    scanner_min_interval_seconds: float
    scanner_max_interval_seconds: float
    scanner_max_bodies: int
    scanner_max_reading_age_seconds: float
    brain: BrainSettings
    fleet: FleetSettings
    defender: DefenderSettings
    telegram: TelegramSettings
    scheduler: SchedulerSettings
    source_file: Path | None = None
    source_version: str | None = None

    @property
    def server_origin(self) -> str:
        return f"https://s{self.server_number}-{self.community}.ogame.gameforge.com"

    @property
    def overview_url(self) -> str:
        return self.server_origin + "/game/index.php?page=ingame&component=overview"


def load_config(path: Path) -> Config:
    try:
        import yaml
    except ImportError as error:
        raise ConfigurationError("Instala las dependencias de modulo1/requirements.txt.") from error
    try:
        source_bytes = path.read_bytes()
        raw = yaml.safe_load(source_bytes.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigurationError("No se pudo leer el archivo YAML.") from error
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ConfigurationError("Se requiere un objeto YAML con version: 1.")
    schema = {
        "account": {"lobby_url", "community", "server_number", "expected_player_name"},
        "browser": {"headless", "channel", "profile_dir", "locale", "navigation_timeout_seconds", "action_timeout_seconds"},
        "session": {"manual_login_timeout_seconds", "health_check_seconds", "max_reconnect_attempts", "initial_backoff_seconds", "max_backoff_seconds"},
        "timing": {"min_delay_seconds", "max_delay_seconds"},
        "logging": {"file", "rotation_mb", "backup_count"},
        "scanner": {"enabled", "output_file", "min_interval_seconds", "max_interval_seconds", "max_bodies_per_scan", "max_reading_age_seconds"},
    }
    if set(raw) != {"version", *schema, "automation", "builder", "research", "fleet", "defender", "telegram", "scheduler"}:
        raise ConfigurationError("Faltan secciones o hay secciones desconocidas.")
    for name, fields in schema.items():
        if not isinstance(raw[name], dict) or set(raw[name]) != fields:
            raise ConfigurationError(f"Campos incorrectos en la sección {name}.")

    def value(section: str, key: str, kind: type, minimum: float | None = None) -> Any:
        item = raw[section][key]
        valid = (type(item) in (int, float)) if kind is float else type(item) is kind
        if not valid or (kind is str and not item.strip()):
            raise ConfigurationError(f"Tipo o valor incorrecto: {section}.{key}.")
        if minimum is not None and (not math.isfinite(item) or item < minimum):
            raise ConfigurationError(f"Valor fuera de rango: {section}.{key}.")
        return float(item) if kind is float else item

    community = value("account", "community", str)
    if not (2 <= len(community) <= 5 and community.isascii() and community.isalpha() and community.islower()):
        raise ConfigurationError("community debe ser un código de comunidad en minúsculas.")
    lobby = value("account", "lobby_url", str)
    if lobby != "https://lobby.ogame.gameforge.com/":
        raise ConfigurationError("El Lobby debe ser el origen oficial de OGame.")
    channel = raw["browser"]["channel"]
    if channel not in (None, "chrome", "msedge", "chromium"):
        raise ConfigurationError("channel debe ser chrome, msedge, chromium o null.")

    def local_path(section: str, key: str) -> Path:
        return (path.resolve().parent / value(section, key, str)).resolve()

    defender, telegram = load_defender_settings(raw["defender"], raw["telegram"], path.resolve().parent)
    config = Config(
        lobby_url=lobby, community=community,
        server_number=value("account", "server_number", int, 1),
        expected_player_name=value("account", "expected_player_name", str).strip(),
        headless=value("browser", "headless", bool), channel=channel,
        profile_dir=local_path("browser", "profile_dir"),
        locale=value("browser", "locale", str),
        navigation_timeout_seconds=value("browser", "navigation_timeout_seconds", float, 1),
        action_timeout_seconds=value("browser", "action_timeout_seconds", float, 0.1),
        manual_login_timeout_seconds=value("session", "manual_login_timeout_seconds", float, 1),
        health_check_seconds=value("session", "health_check_seconds", float, 1),
        max_reconnect_attempts=value("session", "max_reconnect_attempts", int, 1),
        initial_backoff_seconds=value("session", "initial_backoff_seconds", float, 0.1),
        max_backoff_seconds=value("session", "max_backoff_seconds", float, 0.1),
        min_delay_seconds=value("timing", "min_delay_seconds", float, 0),
        max_delay_seconds=value("timing", "max_delay_seconds", float, 0),
        log_file=local_path("logging", "file"),
        rotation_mb=value("logging", "rotation_mb", int, 1),
        backup_count=value("logging", "backup_count", int, 1),
        scanner_enabled=value("scanner", "enabled", bool),
        scanner_output_file=local_path("scanner", "output_file"),
        scanner_min_interval_seconds=value("scanner", "min_interval_seconds", float, 1),
        scanner_max_interval_seconds=value("scanner", "max_interval_seconds", float, 1),
        scanner_max_bodies=value("scanner", "max_bodies_per_scan", int, 1),
        scanner_max_reading_age_seconds=value("scanner", "max_reading_age_seconds", float, 1),
        brain=load_brain_settings(raw, path.resolve().parent),
        fleet=load_fleet_settings(raw["fleet"], path.resolve().parent),
        defender=defender, telegram=telegram,
        scheduler=load_scheduler_settings(raw["scheduler"], path.resolve().parent),
        source_file=path.resolve(),
        source_version=hashlib.sha256(source_bytes).hexdigest(),
    )
    if config.min_delay_seconds > config.max_delay_seconds:
        raise ConfigurationError("El retraso mínimo supera el máximo.")
    if config.initial_backoff_seconds > config.max_backoff_seconds:
        raise ConfigurationError("El backoff inicial supera el máximo.")
    if config.scanner_min_interval_seconds > config.scanner_max_interval_seconds:
        raise ConfigurationError("El intervalo mínimo del scanner supera el máximo.")
    protected = (path.resolve(), config.log_file)
    if config.scanner_output_file in protected or config.scanner_output_file.is_relative_to(config.profile_dir):
        raise ConfigurationError("La salida del scanner no puede sobrescribir configuración, log o perfil.")
    outputs = (config.scanner_output_file, config.brain.journal_file, config.brain.plan_file, config.fleet.plan_file, config.defender.report_file, config.telegram.state_file, config.scheduler.status_file, config.log_file, path.resolve())
    if len(set(outputs)) != len(outputs) or any(output.is_relative_to(config.profile_dir) for output in outputs):
        raise ConfigurationError("Las rutas de configuración, logs, estado, órdenes y planes deben ser distintas y ajenas al perfil.")
    if not config.scanner_enabled and (config.brain.builder_enabled or config.brain.research_enabled):
        raise ConfigurationError("El módulo 3 requiere scanner.enabled: true.")
    if config.fleet.enabled and not config.scanner_enabled:
        raise ConfigurationError("El módulo 4 requiere scanner.enabled: true.")
    return config
