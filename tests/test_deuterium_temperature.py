"""Deuterium stock/level and ROI regression; anonymous observed-level scenario."""
from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import TimeoutError as PlaywrightTimeout

from ogame_bot.adapters.ogame.reader import OGameReader
from ogame_bot.brain.planner import Planner
from ogame_bot.brain.settings import Goal
from ogame_bot.config import load_config
from ogame_bot.domain.models import Reading
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.parsing import BUILDINGS, planet_temperature, resources, technologies
from test_brain import observed, snapshot

ROOT = Path(__file__).resolve().parents[1]


def temperature_html(value: str) -> str:
    return '<div id="planetdata"><span>Temperatura:</span><span>Información</span><span id="temperatureContentField">'+value+'</span></div>'


class DeuteriumTemperatureTests(unittest.TestCase):
    def test_explicit_field_is_read_despite_help_text_and_split_negative_digits(self) -> None:
        doc = Document(temperature_html('De <span>−</span><span>32</span>&nbsp;ºC a <span>8</span>&nbsp;ºC'))
        self.assertEqual(planet_temperature(doc).data, {'minimum': -32, 'maximum': 8})

    def test_celsius_symbols_negative_maximum_and_legacy_markup(self) -> None:
        for value in ('De -44 °C a -4 °C', 'Entre − 44 ℃ y − 4 ℃', '-44º C hasta -4º C'):
            with self.subTest(value=value):
                self.assertEqual(planet_temperature(Document(temperature_html(value))).data, {'minimum': -44, 'maximum': -4})
        legacy = Document('<div id="planetdata">Temperatura: de -44 °C a -4 °C</div>')
        self.assertEqual(planet_temperature(legacy).data['maximum'], -4)

    def test_placeholder_invalid_range_and_duplicate_fields_are_not_guessed(self) -> None:
        for value in ('Cargando', 'De -44 °C', 'De 20 °C a -4 °C', 'De -300 °C a 8 °C'):
            self.assertIsNone(planet_temperature(Document(temperature_html(value))).data)
        duplicate = temperature_html('De -32°C a 8°C').replace('</div>', '<span id="temperatureContentField">De -32°C a 8°C</span></div>')
        self.assertIsNone(planet_temperature(Document(duplicate)).data)

    def test_deuterium_stock_and_level_use_their_own_dom_fields(self) -> None:
        doc = Document('<span id="resources_metal" data-raw="30878">30.878</span>'
            '<span id="resources_crystal" data-raw="9336">9.336</span>'
            '<span id="resources_deuterium" data-raw="6911">6.911</span>'
            '<span id="resources_energy">323</span>'
            '<li class="technology" data-technology="3"><span class="level">6</span></li>')
        self.assertEqual(resources(doc).data['deuterium'], 6911)
        self.assertEqual(technologies(doc, BUILDINGS).data['deuterium_synthesizer'], 6)

    def test_temperature_unblocks_affordable_deuterium_before_more_expensive_mines(self) -> None:
        config = load_config(ROOT / 'config.example.yaml')
        settings = replace(config.brain, strategy='roi', builder_enabled=True, research_enabled=False,
            economy_speed=8, temperatures={}, energy_source='solar_satellite', probe_target=0,
            build_goals=(Goal('metal_mine',20),Goal('crystal_mine',15),Goal('deuterium_synthesizer',11)),
            reserve={'metal':0,'crystal':0,'deuterium':100}, max_payback_days=465,
            resource_weights={'metal':1,'crystal':5/3,'deuterium':2.5})
        state = snapshot()
        body = state.bodies[0]
        body.resources = observed({'metal':30878,'crystal':9336,'deuterium':6911,'energy':323})
        body.buildings = observed({'metal_mine':16,'crystal_mine':13,'deuterium_synthesizer':6,'solar_plant':17})
        body.fields = observed({'used':70,'total':233,'free':163})
        body.storage_capacity = observed({'metal':140000,'crystal':40000,'deuterium':40000})
        planner = Planner(settings,120)
        body.temperature = Reading.unavailable('temperature_range_unknown')
        planner.plan(state)
        self.assertTrue(any(n['reason']=='roi_temperature_unknown' and n['item']=='deuterium_synthesizer' for n in planner.notes))
        body.temperature = planet_temperature(Document(temperature_html('De <span>−</span>32 °C a 8 °C')))
        action = planner.plan(state)[0]
        self.assertEqual((action.item,action.current_level,action.target_level),('deuterium_synthesizer',6,7))
        self.assertEqual(action.cost, {'metal':2562,'crystal':854,'deuterium':0})
        self.assertGreater(action.payback_days,0)


class TemperatureReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_waits_for_temperature_after_planet_fields(self) -> None:
        events: list[str] = []
        page = MagicMock()
        diameter, temperature = MagicMock(), MagicMock()
        diameter.filter.return_value.first.wait_for = AsyncMock(side_effect=lambda **kwargs: events.append('fields'))
        temperature.count = AsyncMock(return_value=1)
        temperature.filter.return_value.wait_for = AsyncMock(side_effect=lambda **kwargs: events.append('temperature'))
        page.locator.side_effect = lambda selector: temperature if selector=='#planetdata #temperatureContentField' else diameter

        async def content() -> str:
            events.append('capture')
            return '<div id="playerName">Test Commander</div>'+temperature_html('De -32 °C a 8 °C')

        page.content = AsyncMock(side_effect=content)

        @asynccontextmanager
        async def read_page(*args):
            yield page

        reader = OGameReader(SimpleNamespace(read_page=read_page,config=SimpleNamespace(expected_player_name='Test Commander')))
        doc = await reader.capture('overview')
        self.assertEqual(events, ['fields','temperature','capture'])
        self.assertEqual(planet_temperature(doc).data['maximum'],8)

    async def test_missing_temperature_does_not_discard_resource_reading(self) -> None:
        page = MagicMock()
        page.locator.return_value.count = AsyncMock(return_value=1)
        page.locator.return_value.filter.return_value.first.wait_for = AsyncMock()
        page.locator.return_value.filter.return_value.wait_for = AsyncMock(side_effect=PlaywrightTimeout('synthetic loading'))
        page.content = AsyncMock(return_value='<div id="playerName">Test Commander</div><div id="resources_deuterium">6.911</div>'+temperature_html('Cargando'))

        @asynccontextmanager
        async def read_page(*args):
            yield page

        reader = OGameReader(SimpleNamespace(read_page=read_page,config=SimpleNamespace(expected_player_name='Test Commander')))
        doc = await reader.capture('overview')
        self.assertEqual(resources(doc).data['deuterium'],6911)
        self.assertIsNone(planet_temperature(doc).data)
