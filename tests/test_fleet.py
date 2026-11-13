"""Cálculos, lectura y transacciones de flota con datos sintéticos."""
from __future__ import annotations

import html
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import Error

from ogame_bot.config import load_config
from ogame_bot.domain.models import BodySnapshot, CelestialBody, Reading, utc_now
from ogame_bot.errors import CoreError
from ogame_bot.fleet.executor import FleetExecutor, matching_flight
from ogame_bot.fleet.models import Flight
from ogame_bot.fleet.planner import FleetPlanner, base_cap, expedition_composition
from ogame_bot.fleet.parsing import slots, outgoing, debris, top1_xml
from ogame_bot.scanner.dom import Document
from ogame_bot.storage.orders import OrderJournal
from test_brain import snapshot, observed, Planner

ROOT = Path(__file__).resolve().parents[1]


def settings():
    config = load_config(ROOT / "config.example.yaml")
    return replace(config.fleet, deuterium_reserve=100, maximum_fuel=1000,
        capacities={"large_cargo": 25000, "pathfinder": 10000, "recycler": 20000},
        expeditions={**config.fleet.expeditions, "enabled": True, "origins": [101], "economy_speed": 8, "top1_source": "manual"})


def account():
    state = snapshot()
    state.bodies[0].ships = observed({"large_cargo": 10000, "pathfinder": 10, "espionage_probe": 10, "recycler": 1000})
    state.registry = observed([state.bodies[0].body, CelestialBody(102, "1:200:9", "moon", "Luna de prueba")])
    state.fleet_slots = observed({"fleet_used": 0, "fleet_total": 16, "expedition_used": 0, "expedition_total": 8})
    state.outgoing = observed([])
    state.top1 = observed({"points": 100000000})
    state.debris = observed([{"coordinates": "1:201:4", "metal": 200000, "crystal": 100000}])
    return state


def rendered(flight: Flight | None = None, *, sent: bool = False) -> str:
    rows = ""
    if flight is not None and sent:
        rows = f'''<div data-fleet-id="77" data-mission-type="{flight.mission}" data-origin="{flight.origin_coordinates}"
        data-destination="{flight.destination}" data-destination-type="{flight.destination_type}" data-return-flight="false"
        data-ships='{html.escape(json.dumps(flight.ships), quote=True)}' data-resources='{html.escape(json.dumps(flight.resources), quote=True)}'></div>'''
    return f'''<meta name="ogame-planet-id" content="101"><span id="playerName">{load_config(ROOT / 'config.example.yaml').expected_player_name}</span>
    <div class="smallplanet"><a class="planetlink" href="/game/index.php?cp=101"><span class="planet-koords">[1:200:8]</span></a></div>
    <div class="smallplanet"><a class="planetlink" href="/game/index.php?cp=103"><span class="planet-koords">[1:200:9]</span></a><a class="moonlink" href="/game/index.php?cp=102"></a></div>
    <div id="resources_metal">1000000</div><div id="resources_crystal">1000000</div><div id="resources_deuterium">1000000</div><div id="resources_energy">10000</div>
    <div id="attack_alert" data-status="noAttack"></div><span class="fleetSlots">0 / 16</span><span class="expeditionSlots">0 / 8</span>
    <div class="technology" data-technology="203"><span class="amount">10000</span></div><div class="technology" data-technology="219"><span class="amount">10</span></div>
    <div class="technology" data-technology="210"><span class="amount">10</span></div><div class="technology" data-technology="209"><span class="amount">1000</span></div>
    <input name="am203" value="999"><div id="fleetMovements" data-loaded="true">{rows}</div>'''


class FleetPlanningTests(unittest.TestCase):
    def test_cap_boundaries_are_explicit(self) -> None:
        self.assertIsNone(base_cap(9999))
        self.assertEqual(base_cap(99999), 500000)
        self.assertEqual(base_cap(100000), 1200000)
        self.assertEqual(base_cap(100000000), 5000000)

    def test_expedition_cargo_and_points_both_constrain_composition(self) -> None:
        fleet = settings()
        ships, target = expedition_composition(fleet, 100000000)
        self.assertGreaterEqual(ships["large_cargo"] * 25000 + 10000, target)
        self.assertEqual((ships["pathfinder"], ships["espionage_probe"]), (1, 1))
        general = replace(fleet, expeditions={**fleet.expeditions, "player_class": "general", "pathfinder": False, "probe": False})
        ships, _ = expedition_composition(general, 100000000)
        self.assertGreaterEqual(ships["large_cargo"] * 60, 25000)

    def test_unknown_capacity_does_not_produce_a_fleet(self) -> None:
        with self.assertRaises(ValueError):
            expedition_composition(replace(settings(), capacities={}), 100000000)

    def test_reserves_and_expedition_slots_are_respected(self) -> None:
        state = account()
        self.assertEqual(FleetPlanner(settings(), 120).plan(state)[0].mission, 15)
        state.fleet_slots.data["expedition_used"] = 1
        self.assertEqual(FleetPlanner(settings(), 120).plan(state), [])
        state.fleet_slots.data["expedition_used"] = 0
        state.fleet_slots.data["fleet_used"] = 15
        self.assertEqual(FleetPlanner(settings(), 120).plan(state), [])

    def test_expired_top1_is_not_rejuvenated(self) -> None:
        state = account()
        state.top1 = Reading({"points": 100000000}, "2000-01-01T00:00:00+00:00", "complete")
        planner = FleetPlanner(settings(), 120)
        self.assertEqual(planner.plan(state), [])
        self.assertEqual(planner.notes[0]["reason"], "top1_unknown_or_expired")

    def test_unknown_outgoing_blocks_planning(self) -> None:
        state = account()
        state.outgoing = Reading.unavailable("loading")
        self.assertEqual(FleetPlanner(settings(), 120).plan(state), [])

    def test_missing_helper_ship_blocks_expedition(self) -> None:
        state = account()
        del state.bodies[0].ships.data["pathfinder"]
        self.assertEqual(FleetPlanner(settings(), 120).plan(state), [])

    def test_transport_only_targets_own_moon_and_keeps_reserves(self) -> None:
        fleet = settings()
        fleet = replace(fleet, expeditions={**fleet.expeditions, "enabled": False}, transports={**fleet.transports, "enabled": True, "sources": [101], "destination_id": 102, "minimum_utilization_percent": 1, "leave_resources": {"metal": 100, "crystal": 200, "deuterium": 300}})
        flight = FleetPlanner(fleet, 120).plan(account())[0]
        self.assertEqual((flight.mission, flight.destination_type, flight.destination_id), (3, 3, 102))
        self.assertLessEqual(flight.resources["metal"], 999900)
        self.assertLessEqual(flight.resources["deuterium"], 1000000 - 300 - fleet.maximum_fuel)
        fleet = replace(fleet, transports={**fleet.transports, "destination_id": 999})
        self.assertEqual(FleetPlanner(fleet, 120).plan(account()), [])

    def test_recycling_respects_radius_and_threshold(self) -> None:
        fleet = settings()
        fleet = replace(fleet, expeditions={**fleet.expeditions, "enabled": False}, recycling={**fleet.recycling, "enabled": True, "origins": [101]})
        self.assertEqual(FleetPlanner(fleet, 120).plan(account())[0].mission, 8)
        fleet = replace(fleet, recycling={**fleet.recycling, "radius_systems": 0})
        self.assertEqual(FleetPlanner(fleet, 120).plan(account()), [])


class FleetParsingTests(unittest.TestCase):
    def test_slots_and_verified_empty_outgoing(self) -> None:
        document = Document(rendered())
        self.assertEqual(slots(document).data["fleet_total"], 16)
        self.assertEqual(outgoing(document).data, [])
        self.assertEqual(outgoing(document).status, "complete")
        self.assertIsNone(outgoing(Document('<div id="fleetMovements"></div>')).data)

    def test_bad_outgoing_row_makes_coverage_partial(self) -> None:
        document = Document(rendered().replace('</div>', '<div data-fleet-id="1"></div></div>', 1))
        # Fila en el contenedor correcto, no un elemento ajeno.
        document = Document(rendered().replace('data-loaded="true">', 'data-loaded="true"><div data-fleet-id="1"></div>'))
        self.assertEqual(outgoing(document).status, "partial")

    def test_debris_requires_verified_system(self) -> None:
        document = Document('<div id="galaxyContent" data-galaxy="1" data-system="201" data-loaded="true"><div data-position="4" data-debris-metal="200.000" data-debris-crystal="100.000"></div></div>')
        self.assertEqual(debris(document, 1, 201).data[0]["metal"], 200000)
        self.assertIsNone(debris(document, 1, 202).data)

    def test_public_rank_retains_source_timestamp(self) -> None:
        payload = b'<highscore category="1" type="0" serverId="es277" timestamp="1805120000"><player position="1" score="123456789"/></highscore>'
        reading = top1_xml(payload, "es277")
        self.assertEqual(reading.data["points"], 123456789)
        self.assertEqual(reading.observed_at, datetime.fromtimestamp(1805120000, timezone.utc).isoformat())
        self.assertIsNone(top1_xml(payload, "es278").data)
        self.assertIsNone(top1_xml(b'<!DOCTYPE evil><highscore/>', "es277").data)

    def test_exact_new_fleet_match_excludes_old_and_ambiguous_rows(self) -> None:
        flight = FleetPlanner(settings(), 120).plan(account())[0]
        rows = outgoing(Document(rendered(flight, sent=True))).data
        self.assertEqual(matching_flight(flight, rows), "77")
        self.assertIsNone(matching_flight(replace(flight, baseline_ids=("77",)), rows))
        self.assertIsNone(matching_flight(flight, rows + [{**rows[0], "id": "78"}]))


class FleetTransactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = load_config(ROOT / "config.example.yaml")
        self.config = replace(config, brain=replace(config.brain, dry_run=False), fleet=settings())
        self.journal = OrderJournal(Path(self.temp.name) / "orders.sqlite3", "test")
        self.addCleanup(self.journal.close)
        self.flight = FleetPlanner(settings(), 120).plan(account())[0]
        self.sent = False
        self.confirm_visible = True
        self.controls = {}
        self.page = MagicMock()
        self.page.content = AsyncMock(side_effect=lambda: rendered(self.flight, sent=self.sent and self.confirm_visible))

        def locator(selector):
            if selector in self.controls:
                return self.controls[selector]
            control = MagicMock()
            control.value = "0"
            control.checked = False
            control.count = AsyncMock(return_value=1)
            control.is_visible = AsyncMock(return_value=True)
            control.is_enabled = AsyncMock(return_value=True)
            control.first.wait_for = AsyncMock()

            async def fill(value): control.value = value
            async def check(): control.checked = True
            control.fill = AsyncMock(side_effect=fill)
            control.select_option = AsyncMock(side_effect=fill)
            control.check = AsyncMock(side_effect=check)
            control.input_value = AsyncMock(side_effect=lambda: control.value)
            control.is_checked = AsyncMock(side_effect=lambda: control.checked)
            control.inner_text = AsyncMock(return_value="100" if selector == self.config.fleet.selectors["fuel"] else "200000000")
            control.get_attribute = AsyncMock(return_value="60")
            control.click = AsyncMock()
            if selector == self.config.fleet.selectors["send"]:
                async def send(**kwargs):
                    self.assertIsNotNone(self.journal.pending("fleet"))
                    self.sent = True
                control.click.side_effect = send
            self.controls[selector] = control
            return control

        self.page.locator.side_effect = locator
        self.session = MagicMock()

        @asynccontextmanager
        async def read_page(*args, **kwargs):
            yield self.page

        self.session.read_page = read_page
        self.executor = FleetExecutor(self.session, self.config, self.journal)

    async def test_send_is_persisted_and_confirmed_by_new_movement(self) -> None:
        result = await self.executor.execute(self.flight)
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["fleet_id"], "77")
        self.assertIsNone(self.journal.pending("fleet"))
        self.controls[self.config.fleet.selectors["send"]].click.assert_awaited_once()

    async def test_unconfirmed_send_is_not_repeated(self) -> None:
        self.confirm_visible = False
        result = await self.executor.execute(self.flight)
        self.assertEqual(result["state"], "pending")
        await self.executor.execute(self.flight)
        self.controls[self.config.fleet.selectors["send"]].click.assert_awaited_once()

    async def test_failed_final_click_stays_uncertain(self) -> None:
        sender = self.page.locator(self.config.fleet.selectors["send"])
        sender.click.side_effect = Error("Synthetic uncertain send")
        with self.assertRaises(Error):
            await self.executor.execute(self.flight)
        self.assertIsNotNone(self.journal.pending("fleet"))
        await self.executor.reconcile()
        sender.click.assert_awaited_once()

    async def test_unknown_fuel_blocks_before_journaling(self) -> None:
        self.page.locator(self.config.fleet.selectors["fuel"]).inner_text.return_value = "1,2 M"
        result = await self.executor.execute(self.flight)
        self.assertEqual(result["state"], "blocked")
        self.assertIsNone(self.journal.pending("fleet"))
        self.assertFalse(self.sent)

    async def test_dry_run_never_fills_or_sends(self) -> None:
        config = replace(self.config, brain=replace(self.config.brain, dry_run=True))
        result = await FleetExecutor(self.session, config, self.journal).execute(self.flight)
        self.assertEqual(result["state"], "simulated")
        self.assertFalse(self.controls)

    async def test_pending_upgrade_does_not_block_a_verified_fleet(self) -> None:
        action = Planner(replace(self.config.brain, research_enabled=False), 120).plan(snapshot())[0]
        self.journal.prepare(action)
        result = await self.executor.execute(self.flight)
        self.assertEqual(result["state"], "confirmed")
        self.assertIsNotNone(self.journal.pending("upgrade"))

    async def test_second_fleet_in_same_lane_is_rejected(self) -> None:
        self.journal.prepare(self.flight)
        with self.assertRaises(CoreError):
            self.journal.prepare(self.flight)


if __name__ == "__main__":
    unittest.main()
