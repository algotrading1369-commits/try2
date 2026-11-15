"""Regression cases from the ES 13.1.0-r5 resource and planet DOM.

Only the relevant markup is reproduced; no account links or session data.
"""
from __future__ import annotations

import html
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ogame_bot.adapters.ogame.reader import OGameReader
from ogame_bot.brain.planner import Planner
from ogame_bot.config import load_config
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.parsing import planet_fields, planet_temperature, resources, storage_capacity
from test_brain import snapshot


def resource_box(resource: str, stock: int, capacity: str) -> str:
    tooltip = (
        '<table class="resourceTooltip"><tr><th>Tu reserva:</th>'
        f'<td><span>{stock}</span></td></tr>'
        '<tr><th>Capacidad de almacenamiento</th>'
        f'<td><span>{capacity}</span></td></tr>'
        '<tr><th>Producción actual:</th><td><span>+5.141</span></td></tr>'
        '<tr><th>Capacidad de camuflaje:</th><td>2.410</td></tr></table>'
    )
    return (
        f'<div id="{resource}_box" class="tooltipHTML resource" '
        f'data-tooltip-title="{html.escape(tooltip, quote=True)}">'
        f'<span id="resources_{resource}" data-raw="{stock}">{stock}</span></div>'
    )


PLANET = '''<div id="planetdata"><div id="planetDetails"><table><tbody>
<tr><td class="desc"><span id="diameterField">Diámetro:</span></td>
<td class="data"><span id="diameterContentField">12.800km (<span>66</span>/<span>233</span>)</span></td></tr>
<tr><td class="desc"><span id="temperatureField">Temperatura:</span></td>
<td class="data"><span id="temperatureContentField">De -32&nbsp;°C a 8&nbsp;°C</span></td></tr>
</tbody></table></div></div>'''


class LiveStorageMarkupTests(unittest.TestCase):
    def test_current_tooltip_reads_capacity_without_confusing_stock_or_production(self) -> None:
        doc = Document(''.join(resource_box(r, n, c) for r, n, c in (
            ('metal', 44499, '140.000'), ('crystal', 34352, '40.000'),
            ('deuterium', 32343, '40.000'),
        )))
        self.assertEqual(storage_capacity(doc).status, 'complete')
        self.assertEqual(storage_capacity(doc).data, {'metal': 140000, 'crystal': 40000, 'deuterium': 40000})
        self.assertEqual(resources(doc).data['crystal'], 34352)

    def test_current_overview_reads_fields_and_temperature(self) -> None:
        doc = Document(PLANET)
        self.assertEqual(planet_fields(doc).data, {'used': 66, 'total': 233, 'free': 167})
        self.assertEqual(planet_temperature(doc).data, {'minimum': -32, 'maximum': 8})

    def test_loading_placeholder_is_not_a_verified_planet(self) -> None:
        doc = Document('<div id="planetdata"><span id="diameterContentField">1_</span></div>')
        self.assertIsNone(planet_fields(doc).data)
        self.assertIsNone(planet_temperature(doc).data)

    def test_conflicting_or_abbreviated_capacity_is_not_guessed(self) -> None:
        for value in ('40k', '40,0 M', '0'):
            self.assertIsNone(storage_capacity(Document(resource_box('metal', 1, value))).data)
        doc = Document(resource_box('metal', 1, '40.000').replace('data-raw="1"', 'data-raw="1" data-max="20000"'))
        self.assertIsNone(storage_capacity(doc).data)

    def test_real_markup_drives_storage_priority_at_threshold_in_both_modes(self) -> None:
        config = load_config(Path(__file__).resolve().parents[1] / 'config.example.yaml')
        for mode in ('roi', 'priority'):
            with self.subTest(mode=mode):
                doc = Document(PLANET + resource_box('metal', 30000, '140.000')
                    + resource_box('crystal', 36000, '40.000')
                    + resource_box('deuterium', 10000, '40.000')
                    + '<span id="resources_energy">210</span>')
                state = snapshot()
                body = state.bodies[0]
                body.resources = resources(doc)
                body.storage_capacity = storage_capacity(doc)
                body.fields = planet_fields(doc)
                body.temperature = planet_temperature(doc)
                body.buildings.data.update(metal_storage=4, crystal_storage=2, deuterium_tank=2)
                settings = replace(config.brain, strategy=mode, research_enabled=False,
                    auto_storage=True, storage_threshold_percent=90,
                    reserve={'metal': 0, 'crystal': 0, 'deuterium': 0})
                action = Planner(settings, 120).plan(state)[0]
                self.assertEqual(action.item, 'crystal_storage')
                self.assertEqual(action.target_level, 3)


class OverviewReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_waits_for_numeric_planet_details_before_reading_html(self) -> None:
        events: list[str] = []
        page = MagicMock()

        async def ready(**kwargs: object) -> None:
            events.append('details_ready')

        async def content() -> str:
            events.append('capture')
            return '<div id="playerName">Test Commander</div>' + PLANET

        page.locator.return_value.filter.return_value.first.wait_for = AsyncMock(side_effect=ready)
        page.content = AsyncMock(side_effect=content)

        @asynccontextmanager
        async def read_page(component: str, body_id: int | None):
            yield page

        session = SimpleNamespace(read_page=read_page,
            config=SimpleNamespace(expected_player_name='Test Commander'))
        document = await OGameReader(session).capture('overview')
        self.assertEqual(events, ['details_ready', 'capture'])
        pattern = page.locator.return_value.filter.call_args.kwargs['has_text']
        self.assertIsNone(pattern.search('1_'))
        self.assertIsNotNone(pattern.search('12.800km (66/233)'))
        self.assertEqual(planet_fields(document).data['free'], 167)
