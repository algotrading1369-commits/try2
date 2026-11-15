"""Adaptador de lectura DOM aislado de los cálculos y la persistencia."""
from __future__ import annotations

import re

from playwright.async_api import Error, Page

from ...core.session import SessionManager
from ...errors import IdentityMismatch, LoginRequired, ReadError
from ...scanner.dom import Document
from ...scanner.parsing import current_body_id


async def expand_event_panel(page: Page) -> None:
    try:
        # La identidad aparece antes de que termine la carga AJAX del resumen.
        await page.locator("#eventboxFilled:visible, #eventboxBlank:visible").first.wait_for(state="visible")
        summary = page.locator("#eventboxFilled")
        if await summary.count() != 1 or not await summary.is_visible():
            return
        # Pulsar el resumen no garantiza que se despliegue el listado.
        opener = page.locator("#js_eventDetailsClosed")
        if await opener.count() == 0:
            opener = summary
        content = page.locator("#eventContent")
        if await opener.count() == 1 and await opener.is_visible() and (
            await content.count() == 0 or not await content.is_visible()
        ):
            await opener.click()
        # La tabla puede ser visible antes de que la respuesta AJAX inserte filas.
        await page.locator("#eventContent .eventFleet").first.wait_for(state="attached")
    except Error:
        # Un timeout conserva el indicador global; nunca lo convierte en "sin ataque".
        return


class OGameReader:
    def __init__(self, session: SessionManager) -> None:
        self.session = session

    async def capture(self, component: str, body_id: int | None = None, *, expand_events: bool = False) -> Document:
        async with self.session.read_page(component, body_id) as page:
            if component == "overview":
                # El marco autenticado aparece antes del detalle AJAX del planeta.
                # Esperar datos numéricos evita capturar el marcador inicial «1_».
                await page.locator("#planetdata #diameterContentField, #planetdata .fields").filter(
                    has_text=re.compile(r"\d+\s*/\s*\d+")
                ).first.wait_for(state="attached")
            elif component == "fleetdispatch":
                # Un planeta sin naves no tiene tarjetas technology.
                await page.locator('.technology[data-technology], #warning p').first.wait_for(state="attached")
            elif component in {"supplies", "facilities", "research", "shipyard"}:
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
