"""Perfil persistente dedicado; no copia cookies de otros navegadores."""
from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any

from playwright.async_api import BrowserContext, Error, Playwright, async_playwright

from ..config import Config
from ..errors import CoreError
from .locking import profile_lock

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.context: BrowserContext | None = None
        self._playwright: Playwright | None = None
        self._lock: Any = None

    async def __aenter__(self) -> BrowserManager:
        self._lock = profile_lock(self.config.profile_dir)
        self._lock.__enter__()
        try:
            self._playwright = await asyncio.wait_for(async_playwright().start(), timeout=30)
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_dir),
                channel=self.config.channel,
                headless=self.config.headless,
                locale=self.config.locale,
                timeout=self.config.navigation_timeout_seconds * 1000,
            )
            self.context.set_default_timeout(self.config.action_timeout_seconds * 1000)
            self.context.set_default_navigation_timeout(self.config.navigation_timeout_seconds * 1000)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        try:
            if self.context is not None:
                try:
                    await self.context.close()
                except Error:
                    logger.warning("El navegador ya estaba cerrado.")
        finally:
            try:
                if self._playwright is not None:
                    await asyncio.wait_for(self._playwright.stop(), timeout=10)
            finally:
                if self._lock is not None:
                    self._lock.__exit__(exc_type, exc, traceback)
                    self._lock = None

    def require_context(self) -> BrowserContext:
        if self.context is None:
            raise CoreError("El navegador todavía no se ha iniciado.")
        return self.context
