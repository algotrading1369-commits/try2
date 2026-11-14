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
        self.process: subprocess.Popen[bytes] | None = None
        self.control_token: str | None = None
        self.stop_requested = False

    def settings(self) -> dict[str, Any]:
        raw_bytes = self.path.read_bytes()
        raw = yaml.safe_load(raw_bytes)
        return {"version": hashlib.sha256(raw_bytes).hexdigest(),
                "automation": {key: raw["automation"].get(key, default) for key, default in
                               (("dry_run", True), ("probe_target", 0), ("probe_body_id", None), ("probe_bootstrap_live", False), ("min_interval_seconds", 60), ("max_interval_seconds", 90))},
                "builder": {key: raw["builder"][key] for key in ("enabled", "priorities", "reserve", "auto_solar")},
                "research": {key: raw["research"][key] for key in ("enabled", "priorities")},
                "defender": {key: raw["defender"][key] for key in ("enabled", "mission", "trigger_seconds")}}

    def process_status(self) -> dict[str, Any]:
        code = self.process.poll() if self.process is not None else None
        owned = self.process is not None and code is None
        status = self.scheduler_status()
        return {"owned_running": owned, "pid": self.process.pid if owned else None,
                "exit_code": code, "stop_requested": self.stop_requested if owned else False,
                "external_running": not owned and status.get("state") == "running" and status.get("heartbeat_is_recent", False)}

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
            return {"application": "ogame-local-panel", "installation": self.installation, "csrf": self.csrf,
                    "account": {"player": self.config.expected_player_name, "universe": self.config.server_number, "community": self.config.community},
                    "settings": self.settings(), "process": self.process_status(),
                    "catalog": {"building": [{"item": key, "name": value.name_es} for key, value in BUILD.items()],
                                "research": [{"item": key, "name": value.name_es} for key, value in RESEARCH.items()]},
                    "scheduler": self.scheduler_status(), "snapshot": snapshot,
                    "defender": load_state(self.config.defender.report_file), "plan": load_state(self.config.brain.plan_file),
                    "diagnostics": diagnose(self.config, snapshot), "orders": self.orders(), "logs": logs}

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            process = self.process_status()
            if process["owned_running"] or process["external_running"]:
                raise PanelError("Detén el bot antes de guardar cambios. Se aplicarán en el próximo arranque.", 409)
            expected = {"version", "automation", "builder", "research", "defender"}
            if set(payload) != expected or payload["version"] != self.settings()["version"]:
                raise PanelError("La configuración cambió. Recarga el panel antes de guardar.", 409)
            raw = yaml.safe_load(self.path.read_bytes())
            allowed = {"automation": {"dry_run", "probe_target", "probe_body_id", "probe_bootstrap_live", "min_interval_seconds", "max_interval_seconds"},
                       "builder": {"enabled", "priorities", "reserve", "auto_solar"},
                       "research": {"enabled", "priorities"}, "defender": {"enabled", "mission", "trigger_seconds"}}
            for section, keys in allowed.items():
                if not isinstance(payload[section], dict) or set(payload[section]) != keys:
                    raise PanelError("Campos de configuración no admitidos.")
                raw[section].update(payload[section])
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

    def start(self) -> dict[str, str]:
        with self.lock:
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
                self.process = subprocess.Popen([sys.executable, "-m", "ogame_bot", "--config", str(self.path)],
                                                cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.DEVNULL,
                                                stdout=output, stderr=subprocess.STDOUT,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return {"message": "Bot iniciado. Entra en Veritate en el navegador que abra el bot."}

    def stop(self) -> dict[str, str]:
        with self.lock:
            if self.process is None or self.process.poll() is not None or self.control_token is None:
                raise PanelError("Este panel no controla un proceso activo. Si lo abriste con BAT, usa Ctrl+C en su consola.", 409)
            save_snapshot(self.config.scheduler.status_file.with_name("panel_stop.json"),
                          {"pid": self.process.pid, "token": self.control_token, "action": "stop"})
            self.stop_requested = True
            return {"message": "Parada solicitada. Esperando a que finalice la acción actual."}
