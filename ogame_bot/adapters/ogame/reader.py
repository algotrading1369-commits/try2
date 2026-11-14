"""Adaptador de lectura DOM aislado de los cálculos y la persistencia."""
from __future__ import annotations

from playwright.async_api import Error, Page

from ...core.session import SessionManager
from ...errors import IdentityMismatch, LoginRequired, ReadError
from ...scanner.dom import Document
from ...scanner.parsing import current_body_id


async def expand_event_panel(page: Page) -> None:
    opener = page.locator("#eventboxFilled")
    content = page.locator("#eventContent")
    if await opener.count() == 1 and await opener.is_visible() and (
        await content.count() == 0 or not await content.is_visible()
    ):
        try:
            await opener.click()
            await content.wait_for(state="visible")
        except Error:
            # La ausencia de eventos verificados no se interpreta como ausencia de ataques.
            pass


class OGameReader:
    def __init__(self, session: SessionManager) -> None:
        self.session = session

    async def capture(self, component: str, body_id: int | None = None, *, expand_events: bool = False) -> Document:
        async with self.session.read_page(component, body_id) as page:
            if component == "fleetdispatch":
                # Un planeta sin naves no tiene tarjetas technology.
                await page.locator('.technology[data-technology], #warning p').first.wait_for(state="attached")
            elif component in {"supplies", "facilities", "research"}:
                # Espera contenido de la sección, no solo el DOM base autenticado.
                await page.locator(".technology[data-technology]").first.wait_for(state="attached")
            if expand_events:
                await expand_event_panel(page)
            document = Document(await page.content())
            names = document.by_id("playerName")
            if len(names) != 1 or not names[0].text:
                raise LoginRequired("La instantánea no contiene una identidad autenticada.")
            if names[0].text != self.session.config.expected_player_name:
                raise IdentityMismatch("La identidad cambió durante la captura del scanner.")
            if body_id is not None and current_body_id(document) != body_id:
                raise ReadError("No se pudo verificar el planeta o luna de la lectura.")
            return document
