"""Parada cooperativa solo para el proceso lanzado por el panel local."""
from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path

from ..storage.snapshots import save_snapshot


def process_identity(pid: int) -> str | None:
    """Read process birth identity; never send a signal to an OS process."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def registered_control(path: Path) -> dict[str, object] | None:
    try:
        if path.stat().st_size > 2048:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or type(data.get("pid")) is not int or data["pid"] <= 0
                or not isinstance(data.get("token"), str) or len(data["token"]) < 20
                or not isinstance(data.get("birth"), str)):
            return None
        return data if process_identity(data["pid"]) == data["birth"] else None
    except (OSError, ValueError, UnicodeError):
        return None


def register_control(directory: Path) -> str:
    token = os.environ.get("OGAME_PANEL_CONTROL_TOKEN") or secrets.token_urlsafe(32)
    os.environ["OGAME_PANEL_CONTROL_TOKEN"] = token
    save_snapshot(directory / "bot_control.json", {"pid": os.getpid(), "token": token,
                                                 "birth": process_identity(os.getpid()), "protocol": 4})
    # A stop may have been requested before the Windows venv launcher handed
    # over to this interpreter. The random launch capability binds that request.
    stop_file = directory / "panel_stop.json"
    try:
        if stop_file.stat().st_size <= 2048:
            pending = json.loads(stop_file.read_text(encoding="utf-8"))
            if (isinstance(pending, dict) and pending.get("action") == "stop"
                    and isinstance(pending.get("token"), str)
                    and hmac.compare_digest(pending["token"], token)):
                save_snapshot(stop_file, {"pid": os.getpid(), "token": token, "action": "stop"})
    except (OSError, ValueError, UnicodeError):
        pass
    return token


def unregister_control(directory: Path, token: str) -> None:
    path = directory / "bot_control.json"
    data = registered_control(path)
    if data is not None and data.get("pid") == os.getpid() and data.get("token") == token:
        path.unlink(missing_ok=True)


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
