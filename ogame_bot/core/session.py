"""Reutilización, comprobación de identidad y recuperación acotada."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import urlencode, urlsplit, parse_qs

from playwright.async_api import BrowserContext, Error, Page

from ..config import Config
from ..errors import BrowserClosed, ConnectionLost, IdentityMismatch, LoginRequired, ReadError
from .timing import backoff, jitter

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    STARTING = "starting"
    WAITING_LOGIN = "waiting_login"
    READY = "ready"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


class SessionManager:
    def __init__(self, context: BrowserContext, config: Config) -> None:
        self.context = context
        self.config = config
        self.page: Page | None = None
        self.state = SessionState.STARTING
        self.action_lock = asyncio.Lock()
        self.defense_paused = False
        self.authentication_generation = 0
        self.before_navigation: Callable[[], Awaitable[None]] | None = None
        self._closed = False
        context.on("close", self._on_close)

    def _on_close(self, *_: object) -> None:
        self._closed = True
        self.state = SessionState.STOPPED

    def _check_open(self) -> None:
        if self._closed or not self.context.pages:
            raise BrowserClosed("Navegador cerrado; sesión detenida.")

    def is_game_url(self, url: str) -> bool:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == urlsplit(self.config.server_origin).netloc
            and parsed.path == "/game/index.php"
        )

    async def _identity(self, page: Page) -> bool:
        if page.is_closed() or not self.is_game_url(page.url):
            return False
        locator = page.locator("#playerName")
        if await locator.count() != 1 or not await locator.is_visible():
            return False
        name = (await locator.inner_text()).strip()
        if not name:
            return False
        if name != self.config.expected_player_name:
            raise IdentityMismatch("La cuenta abierta no coincide con expected_player_name.")
        return True

    async def _find_authenticated_page(self) -> Page | None:
        for page in list(self.context.pages):
            if await self._identity(page):
                return page
        return None

    async def _navigate(self, page: Page, url: str) -> None:
        await jitter(self.config.min_delay_seconds, self.config.max_delay_seconds)
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is not None and response.status >= 400:
            raise ConnectionLost("El servidor devolvió un error HTTP.")

    async def _wait_for_manual_login(self) -> None:
        if self.config.headless:
            raise LoginRequired("Sesión no disponible. Ejecuta primero con headless: false.")
        self.state = SessionState.WAITING_LOGIN
        logger.warning(
            "Inicia sesión manualmente y abre el universo %s, comunidad %s. "
            "La cuenta debe ser %s. Se admiten las nuevas pestañas del Lobby.",
            self.config.server_number, self.config.community, self.config.expected_player_name,
        )
        deadline = time.monotonic() + self.config.manual_login_timeout_seconds
        while time.monotonic() < deadline:
            self._check_open()
            try:
                candidate = await self._find_authenticated_page()
                if candidate is not None:
                    self.page = candidate
                    self.state = SessionState.READY
                    self.authentication_generation += 1
                    logger.info("Sesión e identidad verificadas en el universo configurado.")
                    return
            except Error:
                if self._closed:
                    raise BrowserClosed("Navegador cerrado durante el acceso.") from None
            await asyncio.sleep(1)
        raise LoginRequired("Tiempo de acceso agotado; reinicia cuando puedas iniciar sesión.")

    async def connect(self) -> None:
        async with self.action_lock:
            candidate = await self._find_authenticated_page()
            if candidate is not None:
                self.page = candidate
                await self._recover()
                return
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self._recover()

    async def _recover(self) -> None:
        self.state = SessionState.RECONNECTING
        for attempt in range(self.config.max_reconnect_attempts):
            self._check_open()
            if self.page is None or self.page.is_closed():
                self.page = await self.context.new_page()
            try:
                await self._navigate(self.page, self.config.overview_url)
                try:
                    await self.page.locator("#playerName").wait_for(state="visible")
                except Error:
                    if self._closed:
                        raise BrowserClosed("Navegador cerrado.") from None
                if await self._identity(self.page):
                    self.state = SessionState.READY
                    logger.info("Conexión e identidad verificadas.")
                    return
                # No automatizar formularios, desafíos o selectores del Lobby no comprobados.
                if not self.is_game_url(self.page.url):
                    await self._navigate(self.page, self.config.lobby_url)
                await self._wait_for_manual_login()
                return
            except (Error, ConnectionLost):
                if self._closed:
                    raise BrowserClosed("Navegador cerrado.") from None
                logger.warning("Fallo de conexión, intento %s/%s.", attempt + 1, self.config.max_reconnect_attempts)
                if attempt + 1 < self.config.max_reconnect_attempts:
                    await asyncio.sleep(backoff(attempt, self.config.initial_backoff_seconds, self.config.max_backoff_seconds))
        raise ConnectionLost("Intentos de conexión agotados; sesión detenida.")

    async def check(self) -> None:
        """Comprueba la sesión contra el servidor, no solo contra un DOM antiguo."""
        async with self.action_lock:
            self._check_open()
            await self._recover()

    @asynccontextmanager
    async def read_page(self, component: str, body_id: int | None = None, *, galaxy: int | None = None, system: int | None = None) -> AsyncIterator[Page]:
        """Navegación de lectura exclusiva; verifica identidad y componente tras cargar."""
        if component not in {"overview", "supplies", "facilities", "research", "shipyard", "fleetdispatch", "movement", "galaxy"}:
            raise ReadError("Componente de lectura no admitido.")
        if body_id is not None and (type(body_id) is not int or body_id <= 0):
            raise ReadError("Identificador de cuerpo inválido.")
        if self.before_navigation is not None:
            await self.before_navigation()
        async with self.action_lock:
            self._check_open()
            if self.page is None or self.page.is_closed() or self.state != SessionState.READY:
                raise LoginRequired("Es necesario recuperar la sesión antes de leer.")
            query = {"page": "ingame", "component": component}
            if body_id is not None:
                query["cp"] = str(body_id)
            if galaxy is not None or system is not None:
                if component != "galaxy" or type(galaxy) is not int or type(system) is not int or not 1 <= galaxy <= 99 or not 1 <= system <= 9999:
                    raise ReadError("Coordenadas de galaxia inválidas.")
                query.update({"galaxy": str(galaxy), "system": str(system)})
            try:
                await self._navigate(self.page, self.config.server_origin + "/game/index.php?" + urlencode(query))
                if not self.is_game_url(self.page.url):
                    self.state = SessionState.WAITING_LOGIN
                    raise LoginRequired("El servidor redirigió fuera de la sesión del juego.")
                await self.page.locator("#playerName").wait_for(state="visible")
                if not await self._identity(self.page):
                    self.state = SessionState.WAITING_LOGIN
                    raise LoginRequired("La sesión ha dejado de estar autenticada.")
                if parse_qs(urlsplit(self.page.url).query).get("component") != [component]:
                    raise ReadError("El servidor no mostró el componente solicitado.")
                yield self.page
            except Error as error:
                if self._closed:
                    raise BrowserClosed("Navegador cerrado durante la lectura.") from None
                # Una excepción puede contener URL/tokens; solo se publica su categoría.
                raise ReadError("No se pudo completar la lectura del navegador.") from error

    async def run(self) -> None:
        try:
            await self.connect()
            while True:
                # Intervalos pequeños permiten detectar cierre sin esperar todo el periodo.
                deadline = time.monotonic() + self.config.health_check_seconds
                while time.monotonic() < deadline:
                    self._check_open()
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
                await self.check()
        finally:
            self.state = SessionState.STOPPED
