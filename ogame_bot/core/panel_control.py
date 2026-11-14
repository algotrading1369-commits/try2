"""Parada cooperativa solo para el proceso lanzado por el panel local."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path


def stop_requested(path: Path, token: str | None, pid: int) -> bool:
    if not token:
        return False
    try:
        if path.stat().st_size > 2048:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(data, dict) and data.get("action") == "stop" and type(data.get("pid")) is int
                and data["pid"] == pid and isinstance(data.get("token"), str)
                and hmac.compare_digest(data["token"], token))
    except (OSError, ValueError, UnicodeError):
        return False


def panel_stop_requested(path: Path) -> bool:
    return stop_requested(path, os.environ.get("OGAME_PANEL_CONTROL_TOKEN"), os.getpid())
