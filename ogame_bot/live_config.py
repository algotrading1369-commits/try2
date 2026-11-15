"""Validated instruction updates with an acknowledgement tied to the bot process."""
from __future__ import annotations

import hashlib
import os
from dataclasses import fields
from typing import Any

from .config import Config
from .domain.models import utc_now
from .errors import ConfigurationError
from .storage.snapshots import save_snapshot
from .core.panel_control import process_identity


def config_version(config: Config) -> str | None:
    return hashlib.sha256(config.source_file.read_bytes()).hexdigest() if config.source_file else None


def validate_live_change(previous: Config, candidate: Config) -> None:
    editable = {"scheduler", "brain", "fleet", "defender", "telegram", "scanner_enabled", "scanner_min_interval_seconds", "scanner_max_interval_seconds", "scanner_max_bodies", "scanner_max_reading_age_seconds", "health_check_seconds", "manual_login_timeout_seconds", "max_reconnect_attempts", "initial_backoff_seconds", "max_backoff_seconds", "min_delay_seconds", "max_delay_seconds", "navigation_timeout_seconds", "action_timeout_seconds", "source_file"}
    for field in fields(previous):
        if field.name not in editable | {"source_version"} and getattr(previous, field.name) != getattr(candidate, field.name):
            raise ConfigurationError("Este cambio de cuenta, navegador o almacenamiento requiere reiniciar el bot.")
    for before, after, protected in (
        (previous.brain, candidate.brain, ("journal_file", "plan_file")),
        (previous.fleet, candidate.fleet, ("plan_file", "selectors")),
        (previous.defender, candidate.defender, ("report_file",)),
        (previous.telegram, candidate.telegram, ("state_file", "token_env", "chat_id_env")),
        (previous.scheduler,candidate.scheduler,tuple(field.name for field in fields(previous.scheduler) if field.name != "isolate_failures")),
    ):
        if any(getattr(before, key) != getattr(after, key) for key in protected):
            raise ConfigurationError("Las rutas y credenciales requieren reiniciar el bot.")


def report_config(config: Config, version: str | None, state: str, *, mode: str, error: str | None = None) -> None:
    data: dict[str, Any] = {"pid": os.getpid(), "birth": process_identity(os.getpid()), "version": version, "state": state, "mode": mode, "updated_at": utc_now()}
    if error:
        data["error"] = error
    save_snapshot(config.scheduler.status_file.with_name("config_status.json"), data)
