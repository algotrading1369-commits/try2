"""Alertas con deduplicación persistente. Nunca registra URL, token o respuesta cruda."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable

from ..defender.settings import TelegramSettings
from ..errors import ConfigurationError
from ..storage.snapshots import save_snapshot

logger = logging.getLogger(__name__)


class TelegramTransport:
    def __init__(self, settings: TelegramSettings) -> None:
        self.settings = settings
        self._token = os.environ.get(settings.token_env, "")
        self._chat_id = os.environ.get(settings.chat_id_env, "")
        if settings.enabled and (not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self._token) or not re.fullmatch(r"-?[0-9]+", self._chat_id)):
            raise ConfigurationError("Faltan las variables de entorno válidas para Telegram.")

    def _send(self, text: str) -> bool:
        data = json.dumps({"chat_id": self._chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode("utf-8")
        request = urllib.request.Request("https://api.telegram.org/bot" + self._token + "/sendMessage", data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                payload = json.loads(response.read(100000))
            return isinstance(payload, dict) and payload.get("ok") is True
        except (OSError, ValueError, urllib.error.URLError):
            logger.error("No se pudo entregar la alerta de Telegram.")
            return False

    async def send(self, text: str) -> bool:
        if not self.settings.enabled:
            return False
        return await asyncio.to_thread(self._send, text)


class AlertDispatcher:
    def __init__(self, settings: TelegramSettings, sender: Callable[[str], Awaitable[bool]] | None = None) -> None:
        self.settings = settings
        self.sender = sender or TelegramTransport(settings).send
        self.timestamps: dict[str, float] = {}
        self.pending: dict[str, asyncio.Task[None]] = {}
        if settings.state_file.exists():
            try:
                raw = json.loads(settings.state_file.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("Formato incorrecto")
                self.timestamps = {key: float(stamp) for key, stamp in raw.items() if isinstance(key, str) and type(stamp) in (float, int) and math.isfinite(stamp) and 0 <= stamp <= time.time()}
            except (OSError, ValueError):
                logger.warning("No se pudo recuperar el historial de alertas; se usará uno nuevo.")

    def enqueue(self, key: str, text: str) -> bool:
        digest = hashlib.sha256(("telegram:" if self.settings.enabled else "local:").encode() + key.encode()).hexdigest()
        if digest in self.pending or time.time() - self.timestamps.get(digest, 0) < self.settings.cooldown_seconds:
            return False
        logger.critical("%s", text)
        if len(self.pending) >= 32:
            logger.error("Cola de alertas llena; la alerta sigue registrada en consola.")
            return False

        async def deliver() -> None:
            try:
                delivered = await self.sender(text) if self.settings.enabled else True
                if delivered:
                    self.timestamps[digest] = time.time()
                    self.timestamps = dict(sorted(self.timestamps.items(), key=lambda item: item[1], reverse=True)[:1000])
                    save_snapshot(self.settings.state_file, self.timestamps)
            except Exception:
                # Un error del proveedor puede contener el token en su URL.
                logger.error("Falló la notificación; se mantendrá la posibilidad de reintentar.")
            finally:
                self.pending.pop(digest, None)

        self.pending[digest] = asyncio.create_task(deliver())
        return True

    async def close(self) -> None:
        tasks = list(self.pending.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.settings.timeout_seconds + 1)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
