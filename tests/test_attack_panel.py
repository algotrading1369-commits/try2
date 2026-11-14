"""Contratos mínimos observados en el panel desplegado de ataques ES."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from ogame_bot.adapters.ogame.reader import expand_event_panel
from ogame_bot.defender.threats import resolve_target
from ogame_bot.domain.models import CelestialBody
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.parsing import movements


ATTACK = '''<div id="attack_alert" class="tooltip eventToggle soon"></div>
<table id="eventContent"><tr class="eventFleet" id="eventRow-123"
 data-mission-type="1" data-return-flight="false" data-arrival-time="1805120000">
<td class="countDown"><span class="hostile textBeefy">10m</span></td>
<td class="coordsOrigin"><a>[1:2:3]</a></td>
<td class="destFleet"><span>Destino</span><figure class="planetIcon planet tooltip js_hideTipOnMobile"></figure></td>
<td class="destCoords"><a>[1:2:4]</a></td></tr></table>'''


class AttackParsingTests(unittest.TestCase):
    def test_expanded_attack_resolves_planet_even_with_moon_same_coordinates(self) -> None:
        reading = movements(Document(ATTACK))
        self.assertEqual(reading.status, "complete")
        event = reading.data["events"][0]
        self.assertTrue(event["possible_incoming_attack"])
        self.assertEqual(event["destination_type"], 1)
        self.assertEqual(event["arrival_at"], "2027-03-15T14:13:20+00:00")
        planet = CelestialBody(101, "1:2:4", "planet", "Destino")
        moon = CelestialBody(102, "1:2:4", "moon", "Luna")
        self.assertEqual(resolve_target(event, [moon, planet]), planet)

    def test_unknown_destination_icon_is_not_guessed(self) -> None:
        event = movements(Document(ATTACK.replace("planetIcon planet", "planetIcon unknown"))).data["events"][0]
        self.assertIsNone(event["destination_type"])
        self.assertIsNone(resolve_target(event, [CelestialBody(101, "1:2:4", "planet", "Destino")]))

    def test_conflicting_destination_type_is_not_guessed(self) -> None:
        reading = movements(Document(ATTACK.replace('data-mission-type="1"', 'data-destination-type="3" data-mission-type="1"')))
        self.assertIn("conflicting_destination_type", reading.issues)
        self.assertIsNone(reading.data["events"][0]["destination_type"])


class PanelOpeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_details_control_instead_of_summary(self) -> None:
        button = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), click=AsyncMock())
        content = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=False), wait_for=AsyncMock())
        page = MagicMock()
        page.locator.side_effect = {"#js_eventDetailsClosed": button, "#eventContent": content}.__getitem__
        await expand_event_panel(page)
        button.click.assert_awaited_once()
        content.wait_for.assert_awaited_once_with(state="visible")

    async def test_already_open_panel_is_not_toggled_closed(self) -> None:
        button = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=False), click=AsyncMock())
        page = MagicMock()
        page.locator.return_value = button
        await expand_event_panel(page)
        button.click.assert_not_awaited()
