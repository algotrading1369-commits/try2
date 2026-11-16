"""Visible DOM contract for the observed ES 13.1 two-step expedition form."""
from __future__ import annotations

import re
from playwright.async_api import Page, Locator

from ..errors import ReadError
from ..scanner.dom import Document
from ..scanner.parsing import parse_integer
from .models import Flight, SHIP_IDS

INPUT_NAMES = {
    202: 'transporterSmall', 203: 'transporterLarge', 204: 'fighterLight',
    205: 'fighterHeavy', 206: 'cruiser', 207: 'battleship', 208: 'colonyShip',
    209: 'recycler', 210: 'espionageProbe', 211: 'bomber', 213: 'destroyer',
    214: 'deathstar', 215: 'interceptor', 218: 'reaper', 219: 'explorer',
}


def is_native_form(document: Document) -> bool:
    panels = document.by_id('technologies')
    return len(panels) == 1 and bool(panels[0].find(lambda node: node.tag == 'input' and node.attrs.get('name') == 'transporterLarge'))


def parse_quote(fuel_text: str, capacity_text: str, duration_text: str) -> tuple[int, int, int]:
    fuel_match = re.fullmatch(r'\s*([0-9. ]+)\s*\(\s*[0-9]+(?:[,.][0-9]+)?\s*%\s*\)\s*', fuel_text)
    fuel = parse_integer(fuel_match.group(1)) if fuel_match else parse_integer(fuel_text)
    capacity = parse_integer(capacity_text)
    duration_match = re.fullmatch(r'\s*([0-9]+):([0-5][0-9]):([0-5][0-9])\s*h?\s*', duration_text)
    if fuel is None or capacity is None or duration_match is None:
        raise ReadError('Consumo, carga o duración del formulario no verificables.')
    hours, minutes, seconds = map(int, duration_match.groups())
    duration = hours * 3600 + minutes * 60 + seconds
    if capacity <= 0 or duration <= 0:
        raise ReadError('El cálculo del vuelo todavía no está disponible.')
    return fuel, capacity, duration


class NativeFleetForm:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def control(self, selector: str) -> Locator:
        locator = self.page.locator(selector)
        if await locator.count() != 1:
            raise ReadError('Control del formulario de dos pasos ausente o ambiguo: ' + selector)
        return locator

    async def ships(self, flight: Flight) -> None:
        if flight.mission != 15 or flight.destination_type != 1 or flight.purpose != 'routine':
            raise ReadError('El formulario de dos pasos está validado por ahora para expediciones.')
        document = Document(await self.page.content())
        panels = document.by_id('technologies')
        if len(panels) != 1:
            raise ReadError('Panel de naves ambiguo.')
        inputs = panels[0].find(lambda node: node.tag == 'input')
        known_names = set(INPUT_NAMES.values())
        if any(node.attrs.get('name') not in known_names for node in inputs):
            raise ReadError('Campo de nave no reconocido en el formulario.')
        expected = {INPUT_NAMES[SHIP_IDS[ship]]: count for ship, count in flight.ships.items()}
        for node in inputs:
            name = node.attrs['name']
            control = await self.control(f'#technologies input[name="{name}"]')
            count = expected.get(name, 0)
            if await control.is_enabled():
                await control.fill(str(count))
            elif count or await control.input_value() not in {'', '0'}:
                raise ReadError('Una nave requerida está deshabilitada o conserva una selección.')
        await self.verify_ships(flight)
        await (await self.control('#continueToFleet2')).click()
        await self.page.locator('#galaxy').wait_for(state='visible')

    async def prepare(self, flight: Flight) -> None:
        from .models import coordinates
        for name, value in zip(('galaxy', 'system', 'position'), coordinates(flight.destination)):
            control = await self.control('#' + name)
            await control.fill(str(value))
            # OGame recalculates the target on keyboard/change events.
            await control.press('Tab')
        await (await self.control('#pbutton')).click()
        mission = self.page.locator('#missionButton15[data-mission="15"]')
        await mission.wait_for(state='visible')
        await (await self.control('#missionButton15[data-mission="15"]')).click()
        slider = await self.control('#speedPercentage')
        if await slider.get_attribute('stepsize') != '10' or flight.speed_percent % 10:
            raise ReadError('Escala de velocidad del formulario no reconocida.')
        await (await self.control(f'#speedPercentage .step[data-step="{flight.speed_percent // 10}"]')).click()
        holding = await self.control('select#expeditiontime')
        await holding.select_option(str(flight.holding_hours), force=True)
        for resource, amount in flight.resources.items():
            await (await self.control(f'input#{resource}[name="{resource}"]')).fill(str(amount))
        await self.page.wait_for_function("""() => {
            const name = document.querySelector('#missionName');
            const duration = document.querySelector('#duration');
            return name && name.textContent.trim() === 'Expedición' && duration &&
                /[1-9]/.test(duration.textContent);
        }""")
        await self.verify(flight)

    async def verify_ships(self, flight: Flight) -> None:
        expected = {INPUT_NAMES[SHIP_IDS[ship]]: count for ship, count in flight.ships.items()}
        for name in INPUT_NAMES.values():
            selector = f'#technologies input[name="{name}"]'
            control = self.page.locator(selector)
            count = await control.count()
            if count == 0 and name not in expected:
                continue
            if count != 1:
                raise ReadError('Composición de naves ausente o ambigua.')
            actual = await control.input_value()
            amount = expected.get(name, 0)
            if actual != str(amount) and not (amount == 0 and actual == ''):
                raise ReadError('El formulario contiene una nave no prevista.')

    async def verify(self, flight: Flight) -> None:
        from .models import coordinates
        await self.verify_ships(flight)
        for name, value in zip(('galaxy', 'system', 'position'), coordinates(flight.destination)):
            if await (await self.control('#'+name)).input_value() != str(value):
                raise ReadError('El destino del formulario no coincide.')
        if (await (await self.control('#pbutton')).get_attribute('class') or '').split() != ['planet_selected']:
            raise ReadError('El tipo de destino no está verificado.')
        if (await (await self.control('#missionName')).inner_text()).strip() != 'Expedición':
            raise ReadError('La misión de expedición no está seleccionada.')
        if (await (await self.control('#speedPercentage .step.selected')).inner_text()).strip() != str(flight.speed_percent):
            raise ReadError('La velocidad seleccionada no coincide.')
        if await (await self.control('select#expeditiontime')).input_value() != str(flight.holding_hours):
            raise ReadError('La permanencia de expedición no coincide.')
        for resource, amount in flight.resources.items():
            actual = await (await self.control(f'input#{resource}[name="{resource}"]')).input_value()
            if parse_integer(actual) != amount:
                raise ReadError('La carga del formulario no coincide.')
        food = self.page.locator('input#food[name="food"]')
        if await food.count() != 1 or parse_integer(await food.input_value()) != 0:
            raise ReadError('Carga de comida no prevista o desconocida.')

    async def quote(self) -> tuple[int, int, int]:
        return parse_quote(await (await self.control('#consumption')).inner_text(),
                           await (await self.control('#storage')).inner_text(),
                           await (await self.control('#duration')).inner_text())
