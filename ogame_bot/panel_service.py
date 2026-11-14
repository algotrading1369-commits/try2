"""Estado y controles locales del panel, sin exponer el perfil del navegador."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import Config, load_config
from .brain.catalog import BUILD, RESEARCH
from .readiness import diagnose, load_state
from .scheduler.loop import read_status
from .storage.snapshots import save_snapshot
from .core.panel_control import registered_control, stop_requested as control_stop_requested
from .core.locking import profile_lock
from .errors import CoreError

EDITABLE = {
    "automation": {"dry_run", "economic_live", "probe_target", "probe_body_id", "probe_bootstrap_live", "min_interval_seconds", "max_interval_seconds"},
    "builder": {"enabled", "strategy", "priorities", "reserve", "minimum_energy", "auto_solar", "max_solar_level", "max_payback_days", "economy_speed", "resource_weights", "production_multipliers", "temperatures"},
    "research": {"enabled", "priorities"},
    "fleet": {"enabled", "min_interval_seconds", "max_interval_seconds", "reserved_slots", "deuterium_reserve", "maximum_fuel", "maximum_oneway_seconds", "speed_percent", "capacities", "expeditions", "transports", "recycling"},
    "defender": {"enabled", "origins", "destinations", "mission", "speed_percent", "trigger_seconds", "minimum_lead_seconds", "max_state_age_seconds", "minimum_flight_seconds", "maximum_flight_seconds", "maximum_fuel", "resource_policy", "pause_on_unknown", "idle_min_seconds", "idle_max_seconds", "alert_min_seconds", "alert_max_seconds"},
    "scanner": {"enabled", "min_interval_seconds", "max_interval_seconds", "max_bodies_per_scan", "max_reading_age_seconds"},
    "session": {"manual_login_timeout_seconds", "health_check_seconds", "max_reconnect_attempts", "initial_backoff_seconds", "max_backoff_seconds"},
    "timing": {"min_delay_seconds", "max_delay_seconds"},
    "browser": {"headless", "channel", "locale", "navigation_timeout_seconds", "action_timeout_seconds"},
    "telegram": {"enabled", "timeout_seconds", "cooldown_seconds"},
}


class PanelError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class PanelService:
    def __init__(self, config_path: Path) -> None:
        self.path = config_path.resolve()
        self.config = load_config(self.path)
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)
        self.installation = hashlib.sha256(str(self.path).encode()).hexdigest()[:16]
        project = Path(__file__).parent
        digest = hashlib.sha256()
        for relative in ("panel.py", "panel_service.py", "core/panel_control.py", "core/session.py", "core/browser.py", "panel_assets/index.html", "panel_assets/panel.js", "panel_assets/panel_forms.js"):
            digest.update((project / relative).read_bytes())
        self.backend_build = digest.hexdigest()[:20]
        self.process: subprocess.Popen[bytes] | None = None
        self.control_token: str | None = None
        self.stop_requested = False

    def settings(self) -> dict[str, Any]:
        raw_bytes = self.path.read_bytes()
        raw = yaml.safe_load(raw_bytes)
        raw["automation"].setdefault("probe_target", 0)
        raw["automation"].setdefault("probe_body_id", None)
        raw["automation"].setdefault("probe_bootstrap_live", False)
        raw["automation"].setdefault("economic_live", False)
        return {"version": hashlib.sha256(raw_bytes).hexdigest(),
                **{section: {key: raw[section][key] for key in sorted(keys)} for section, keys in EDITABLE.items()}}

    def process_status(self) -> dict[str, Any]:
        code = self.process.poll() if self.process is not None else None
        owned = self.process is not None and code is None
        control = registered_control(self.config.scheduler.status_file.with_name("bot_control.json"))
        controllable = owned or control is not None
        legacy_active = False
        if not controllable and self.config.profile_dir.with_name(self.config.profile_dir.name + ".lock").exists():
            try:
                with profile_lock(self.config.profile_dir):
                    pass
            except (CoreError, OSError):
                legacy_active = True
        # On Windows the venv executable can be a launcher with a different PID
        # from the Python interpreter that actually runs the bot.
        target_pid = control["pid"] if control else self.process.pid if owned else None
        target_token = control["token"] if control else self.control_token if owned else None
        requested = (control_stop_requested(self.config.scheduler.status_file.with_name("panel_stop.json"), target_token, target_pid)
                     if controllable and target_token else False)
        return {"owned_running": owned, "pid": target_pid,
                "exit_code": code, "stop_requested": requested,
                "can_stop": controllable, "registered_running": control is not None,
                "external_running": not owned and (control is not None or legacy_active)}

    def scheduler_status(self) -> dict[str, Any]:
        from .errors import CoreError
        try:
            return read_status(self.config.scheduler)
        except CoreError:
            return {"state": "unknown", "jobs": [], "heartbeat_is_recent": False}

    def orders(self) -> list[dict[str, Any]]:
        account = self.config.server_origin + "|" + self.config.expected_player_name
        base = self.config.brain.journal_file
        digest = hashlib.sha256(account.encode()).hexdigest()[:20]
        paths = [base, base.with_name(base.stem + "-" + digest + (base.suffix or ".sqlite3"))]
        for path in paths:
            if not path.exists():
                continue
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    identity = db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
                    if identity is None or identity[0] != account:
                        continue
                    rows = db.execute("SELECT state,action,updated_at FROM orders ORDER BY updated_at DESC,rowid DESC LIMIT 12").fetchall()
                    result = []
                    for state, payload, updated in rows:
                        action = json.loads(payload)
                        result.append({"state": state, "kind": action.get("kind"), "item": action.get("item"),
                                       "target": action.get("target_level"), "mission": action.get("mission"), "updated_at": updated})
                    return result
            except (sqlite3.Error, ValueError, OSError, AttributeError):
                continue
        return []

    def state(self) -> dict[str, Any]:
        with self.lock:
            self.config = load_config(self.path)
            snapshot = load_state(self.config.scanner_output_file)
            identity_ok = snapshot is not None and snapshot.get("player") == self.config.expected_player_name and snapshot.get("server") == self.config.server_origin
            if not identity_ok:
                snapshot = None
            try:
                with self.config.log_file.open("rb") as handle:
                    handle.seek(max(0, handle.seek(0, 2) - 40000))
                    logs = handle.read().decode("utf-8", errors="replace").splitlines()[-100:]
            except OSError:
                logs = []
            try:
                console_path = self.config.scheduler.status_file.with_name("panel_bot_console.log")
                with console_path.open("rb") as handle:
                    handle.seek(max(0, handle.seek(0, 2) - 12000))
                    console_logs = handle.read().decode("utf-8", errors="replace").splitlines()[-30:]
            except OSError:
                console_logs = []
            return {"application": "ogame-local-panel", "installation": self.installation, "backend_build": self.backend_build, "control_protocol": 3, "csrf": self.csrf,
                    "account": {"player": self.config.expected_player_name, "universe": self.config.server_number, "community": self.config.community},
                    "settings": self.settings(), "process": self.process_status(),
                    "catalog": {"building": [{"item": key, "name": value.name_es} for key, value in BUILD.items()],
                                "research": [{"item": key, "name": value.name_es} for key, value in RESEARCH.items()]},
                    "scheduler": self.scheduler_status(), "snapshot": snapshot,
                    "defender": load_state(self.config.defender.report_file), "plan": load_state(self.config.brain.plan_file),
                    "fleet_plan": load_state(self.config.fleet.plan_file),
                    "diagnostics": diagnose(self.config, snapshot), "orders": self.orders(), "logs": logs, "console_logs": console_logs}

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            process = self.process_status()
            if process["owned_running"] or process["external_running"]:
                raise PanelError("Detén el bot antes de guardar cambios. Se aplicarán en el próximo arranque.", 409)
            expected = {"version", *EDITABLE}
            if set(payload) != expected or payload["version"] != self.settings()["version"]:
                raise PanelError("La configuración cambió. Recarga el panel antes de guardar.", 409)
            raw = yaml.safe_load(self.path.read_bytes())
            for section, keys in EDITABLE.items():
                if not isinstance(payload[section], dict) or set(payload[section]) != keys:
                    raise PanelError("Campos de configuración no admitidos.")
                raw[section].update(payload[section])
            temperatures = raw["builder"]["temperatures"]
            if not isinstance(temperatures, dict):
                raise PanelError("Las temperaturas deben ser un objeto de IDs de planeta y valores numéricos.")
            converted: dict[int, Any] = {}
            for key, value in temperatures.items():
                if type(key) is int and key > 0:
                    identifier = key
                elif isinstance(key, str) and key.isascii() and key.isdecimal() and str(int(key)) == key and int(key) > 0:
                    identifier = int(key)
                else:
                    raise PanelError("Cada temperatura requiere un ID de planeta entero positivo.")
                converted[identifier] = value
            raw["builder"]["temperatures"] = converted
            if raw["automation"]["dry_run"] is not True:
                raise PanelError("El envío real de flotas sigue pendiente de validación; conserva la simulación.")
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix="panel-candidate-", suffix=".yaml", delete=False) as handle:
                    temporary = Path(handle.name)
                    yaml.safe_dump(raw, handle, allow_unicode=True, sort_keys=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                candidate = load_config(temporary)
                if hashlib.sha256(self.path.read_bytes()).hexdigest() != payload["version"]:
                    raise PanelError("La configuración cambió durante la validación. Recarga el panel.", 409)
                backup = self.config.scheduler.status_file.parent / ("config-panel-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3) + ".yaml")
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(self.path.read_bytes())
                os.replace(temporary, self.path)
                self.config = candidate
                return {"message": "Configuración guardada. Se aplicará al iniciar el bot.", "settings": self.settings()}
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def start(self, mode: str = "full") -> dict[str, str]:
        with self.lock:
            flags = {"full": [], "scanner": ["--scanner-only"], "session": ["--session-only"], "defender": ["--defender-only"]}
            if mode not in flags:
                raise PanelError("Modo de ejecución desconocido.")
            status = self.process_status()
            if status["owned_running"] or status["external_running"]:
                raise PanelError("El bot ya está activo. No se iniciará una segunda instancia.", 409)
            self.config = load_config(self.path)
            self.control_token = secrets.token_urlsafe(32)
            self.stop_requested = False
            env = {**os.environ, "OGAME_PANEL_CONTROL_TOKEN": self.control_token}
            path = self.config.scheduler.status_file.parent / "panel_bot_console.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as output:
                self.process = subprocess.Popen([sys.executable, "-m", "ogame_bot", *flags[mode], "--config", str(self.path)],
                                                cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.DEVNULL,
                                                stdout=output, stderr=subprocess.STDOUT,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return {"message": "Bot iniciado. Entra en el universo configurado desde el navegador del bot."}

    def stop(self) -> dict[str, str]:
        with self.lock:
            control = registered_control(self.config.scheduler.status_file.with_name("bot_control.json"))
            if control is not None:
                pid, token = control["pid"], control["token"]
            elif self.process is not None and self.process.poll() is None and self.control_token:
                pid, token = self.process.pid, self.control_token
            else:
                raise PanelError("No hay un bot con control de parada registrado. Si sigue abierta una versión anterior, detenla una vez con Ctrl+C y arráncala de nuevo.", 409)
            save_snapshot(self.config.scheduler.status_file.with_name("panel_stop.json"),
                          {"pid": pid, "token": token, "action": "stop"})
            self.stop_requested = True
            return {"message": "Parada solicitada. Esperando a que finalice la acción actual."}
