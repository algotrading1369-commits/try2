"""Amenazas, evacuación, notificaciones y supervisión con datos sintéticos."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import URLError

from ogame_bot.config import load_config
from ogame_bot.core.session import SessionState
from ogame_bot.defender.planner import EscapePlanner
from ogame_bot.defender.service import Defender
from ogame_bot.defender.threats import analyze, escape_guard
from ogame_bot.domain.models import CelestialBody, Reading
from ogame_bot.errors import CoreError, ConfigurationError, ReadError
from ogame_bot.fleet.executor import FleetExecutor
from ogame_bot.fleet.models import SHIP_IDS
from ogame_bot.observability.telegram import AlertDispatcher, TelegramTransport
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner import parsing
from ogame_bot.storage.orders import OrderJournal
from test_brain import observed
from test_fleet import account, rendered, settings as fleet_settings
import test_fleet

ROOT = Path(__file__).resolve().parents[1]


def config():
    original = load_config(ROOT / "config.example.yaml")
    return replace(original, fleet=fleet_settings(), defender=replace(original.defender, destinations=(102,), maximum_fuel=1000), brain=replace(original.brain, dry_run=False))


def threatened(seconds=300):
    state = account()
    state.bodies[0].ships = observed({ship: state.bodies[0].ships.data.get(ship, 0) for ship in SHIP_IDS})
    arrival = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
    event = {"id": "eventRow-99", "mission": 1, "returning": False, "hostile": True,
        "arrival_at": arrival, "destination": "1:200:8", "destination_type": 1, "destination_id": 101,
        "possible_incoming_attack": True}
    state.movements = observed({"threat_status": "detected", "events": [event]})
    return state


def escape_plan(state=None):
    state = state or threatened()
    cfg = config()
    _, threats = analyze(state.movements, state.registry, cfg.defender)
    planner = EscapePlanner(cfg.defender, {**cfg.fleet.capacities, "espionage_probe": 0}, cfg.brain.resource_weights)
    return planner.plan(state, threats[0])


def defense_html(flight, *, sent=False, clear=False):
    text = rendered(flight, sent=sent)
    known = {203, 219, 210, 209}
    for identifier in SHIP_IDS.values():
        if identifier not in known:
            text += f'<div class="technology" data-technology="{identifier}"><span class="amount">0</span></div>'
    if clear:
        return text
    timestamp = int(datetime.fromisoformat(flight.impact_at).timestamp())
    return text.replace('data-status="noAttack"', 'data-status="soon"') + f'''<table id="eventContent"><tr class="eventFleet hostile" id="eventRow-99" data-mission-type="1" data-return-flight="false" data-arrival-time="{timestamp}" data-destination-type="1" data-destination-id="101"><td class="destCoords">[1:200:8]</td></tr></table>'''


class ThreatTests(unittest.TestCase):
    def test_known_body_and_window_are_eligible(self):
        state = threatened()
        status, threats = analyze(state.movements, state.registry, config().defender)
        self.assertEqual(status, "detected")
        self.assertTrue(threats[0].eligible)
        self.assertEqual(threats[0].body_id, 101)

    def test_planet_and_moon_are_not_inferred_from_coordinate_alone(self):
        state = threatened()
        state.registry.data.append(CelestialBody(104, "1:200:8", "moon", "Otra luna"))
        event = state.movements.data["events"][0]
        event["destination_id"] = None
        event["destination_type"] = None
        _, threats = analyze(state.movements, state.registry, config().defender)
        self.assertFalse(threats[0].eligible)
        self.assertIsNone(threats[0].body_id)
        event["destination_type"] = 3
        _, threats = analyze(state.movements, state.registry, config().defender)
        self.assertEqual(threats[0].body_id, 104)

    def test_missing_arrival_or_direction_does_not_trigger_escape(self):
        for key in ("arrival_at", "returning"):
            state = threatened()
            state.movements.data["events"][0][key] = None
            _, threats = analyze(state.movements, state.registry, config().defender)
            self.assertFalse(threats[0].eligible)

    def test_too_late_and_too_early_are_not_eligible(self):
        for seconds in (10, 1200, -5):
            state = threatened(seconds)
            _, threats = analyze(state.movements, state.registry, config().defender)
            self.assertFalse(threats[0].eligible)

    def test_stale_reading_is_unknown(self):
        state = threatened()
        state.movements = Reading(state.movements.data, "2000-01-01T00:00:00+00:00", "complete")
        self.assertEqual(analyze(state.movements, state.registry, config().defender), ("unknown", []))

    def test_guard_rejects_cancelled_and_changed_attack(self):
        flight = escape_plan()
        self.assertIsNone(escape_guard(Document(defense_html(flight)), flight, config().defender))
        self.assertEqual(escape_guard(Document(defense_html(flight, clear=True)), flight, config().defender), "threat_no_longer_verified")
        changed = defense_html(flight).replace('data-destination-type="1"', 'data-destination-type="3"')
        self.assertEqual(escape_guard(Document(changed), flight, config().defender), "threat_changed_or_unverified")


class EscapePlanningTests(unittest.TestCase):
    def test_takes_all_confirmed_movable_ships_in_deployment(self):
        state = threatened()
        flight = escape_plan(state)
        self.assertEqual(flight.mission, 4)
        self.assertEqual(flight.purpose, "defender")
        self.assertEqual(flight.ships, {name: count for name, count in state.bodies[0].ships.data.items() if count > 0})
        self.assertEqual(flight.destination_id, 102)
        self.assertGreaterEqual(flight.minimum_duration_seconds, 1800)

    def test_unknown_ship_count_blocks_evacuating_partial_fleet(self):
        state = threatened()
        del state.bodies[0].ships.data["deathstar"]
        self.assertIsNone(escape_plan(state))

    def test_last_reserved_slot_is_available_to_defense(self):
        state = threatened()
        state.fleet_slots = Reading({"fleet_total": 16, "fleet_used": 15}, state.fleet_slots.observed_at, "partial", ("expedition_slots_unknown",))
        self.assertIsNotNone(escape_plan(state))
        state.fleet_slots.data["fleet_used"] = 16
        self.assertIsNone(escape_plan(state))

    def test_attacked_destination_is_excluded(self):
        state = threatened()
        state.movements.data["events"].append({**state.movements.data["events"][0], "id": "eventRow-100", "destination": "1:200:9", "destination_id": 102, "destination_type": 3})
        self.assertIsNone(escape_plan(state))

    def test_missing_capacity_blocks_without_guessing(self):
        state = threatened()
        cfg = config()
        _, threats = analyze(state.movements, state.registry, cfg.defender)
        self.assertIsNone(EscapePlanner(cfg.defender, {}, cfg.brain.resource_weights).plan(state, threats[0]))

    def test_require_all_and_maximize_value_are_distinct(self):
        state = threatened()
        state.bodies[0].ships = observed({ship: 1 if ship == "large_cargo" else 0 for ship in SHIP_IDS})
        cfg = config()
        _, threats = analyze(state.movements, state.registry, cfg.defender)
        planner = EscapePlanner(cfg.defender, {"large_cargo": 25000}, cfg.brain.resource_weights)
        flight = planner.plan(state, threats[0])
        self.assertEqual(flight.resources, {"metal": 0, "crystal": 0, "deuterium": 24000})
        self.assertIsNone(EscapePlanner(replace(cfg.defender, resource_policy="require_all"), {"large_cargo": 25000}, cfg.brain.resource_weights).plan(state, threats[0]))


class DefensiveTransactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_fleet.FleetTransactionTests.setUp(self)
        self.config = config()
        self.flight = escape_plan()
        self.page.content.side_effect = lambda: defense_html(self.flight, sent=self.sent and self.confirm_visible)
        self.executor = FleetExecutor(self.session, self.config, self.journal)
        self.page.locator(self.config.fleet.selectors["flight_seconds"]).get_attribute.return_value = "1800"
        self.page.locator("#eventContent").count.return_value = 1
        self.page.locator("#eventContent").is_visible.return_value = True
        async def send(**kwargs):
            pending = self.journal.pending("fleet")
            self.assertIsNotNone(pending)
            self.flight = pending[1]
            self.sent = True
        self.page.locator(self.config.fleet.selectors["send"]).click.side_effect = send

    async def test_verified_threat_allows_defense_while_normal_actions_pause(self):
        self.session.defense_paused = True
        result = await self.executor.execute(self.flight)
        # La carga definitiva incluye el combustible real, no la cota estimada.
        self.assertEqual(result["effective_action"]["resources"]["deuterium"], 999900)
        self.assertTrue(self.sent)
        self.assertEqual(result["state"], "confirmed")
        self.controls[self.config.fleet.selectors["send"]].click.assert_awaited_once()

    async def test_too_short_flight_is_not_sent(self):
        self.page.locator(self.config.fleet.selectors["flight_seconds"]).get_attribute.return_value = "30"
        result = await self.executor.execute(self.flight)
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(self.sent)
        self.assertIsNone(self.journal.pending("fleet"))

    async def test_cancelled_threat_does_not_send_even_when_global_pause_remains(self):
        self.page.content.side_effect = lambda: defense_html(self.flight, clear=True)
        result = await self.executor.execute(self.flight)
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(self.sent)


class AlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = replace(config().telegram, enabled=True, state_file=Path(self.temp.name) / "alerts.json")

    async def test_success_is_deduplicated_across_restarts(self):
        sender = AsyncMock(return_value=True)
        dispatcher = AlertDispatcher(self.settings, sender)
        self.assertTrue(dispatcher.enqueue("attack:1", "Aviso sintético"))
        self.assertFalse(dispatcher.enqueue("attack:1", "Aviso sintético"))
        await dispatcher.close()
        self.assertFalse(AlertDispatcher(self.settings, sender).enqueue("attack:1", "Aviso sintético"))
        sender.assert_awaited_once()
        self.assertNotIn("Aviso sintético", self.settings.state_file.read_text())

    async def test_failed_delivery_can_be_retried(self):
        sender = AsyncMock(return_value=False)
        dispatcher = AlertDispatcher(self.settings, sender)
        dispatcher.enqueue("attack:1", "Aviso sintético")
        await dispatcher.close()
        self.assertTrue(dispatcher.enqueue("attack:1", "Aviso sintético"))
        await dispatcher.close()
        self.assertEqual(sender.await_count, 2)

    async def test_disabled_telegram_never_calls_network_sender(self):
        sender = AsyncMock(return_value=True)
        dispatcher = AlertDispatcher(replace(self.settings, enabled=False), sender)
        dispatcher.enqueue("attack:1", "Aviso local")
        await dispatcher.close()
        sender.assert_not_awaited()

    async def test_network_error_does_not_log_token(self):
        with patch.dict("os.environ", {self.settings.token_env: "123456:EXAMPLE_TOKEN", self.settings.chat_id_env: "1234"}):
            transport = TelegramTransport(self.settings)
        with patch("urllib.request.urlopen", side_effect=URLError("EXAMPLE_TOKEN")), self.assertLogs("ogame_bot.observability.telegram", level="ERROR") as logs:
            self.assertFalse(transport._send("Prueba"))
        self.assertNotIn("EXAMPLE_TOKEN", " ".join(logs.output))

    async def test_enabled_telegram_requires_environment_credentials(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ConfigurationError):
            TelegramTransport(self.settings)


class DefenderServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = config()
        self.config = replace(cfg, defender=replace(cfg.defender, report_file=Path(self.temp.name) / "defender.json"), fleet=replace(cfg.fleet, capacities={**cfg.fleet.capacities, "espionage_probe": 0}), brain=replace(cfg.brain, dry_run=True))
        self.journal = OrderJournal(Path(self.temp.name) / "orders.sqlite3", "test")
        self.addCleanup(self.journal.close)
        self.session = MagicMock()
        self.session.defense_paused = False
        self.alerts = MagicMock()
        self.defender = Defender(self.session, self.config, self.journal, self.alerts)
        self.flight = escape_plan()
        self.defender.reader = MagicMock()
        self.defender.reader.capture = AsyncMock(side_effect=lambda *args, **kwargs: Document(defense_html(self.flight)))
        self.defender.executor = MagicMock()
        self.defender.executor.execute = AsyncMock(return_value={"state": "simulated"})
        self.defender.executor.reconcile = AsyncMock(return_value={"state": "pending"})

    async def test_simulated_defense_pauses_jobs_and_labels_notification(self):
        report = await self.defender.process()
        self.assertTrue(report["paused"])
        self.assertEqual(report["result"]["state"], "simulated")
        self.defender.executor.execute.assert_awaited_once()
        self.assertTrue(any("SIMULACIÓN" in call.args[1] for call in self.alerts.enqueue.call_args_list))
        self.assertEqual(json.loads(self.config.defender.report_file.read_text())["state"], "detected")

    async def test_read_failure_is_unknown_and_pauses_routine_work(self):
        self.defender.reader.capture.side_effect = ReadError("Synthetic missing event panel")
        report = await self.defender.process()
        self.assertEqual(report["state"], "unknown")
        self.assertTrue(self.session.defense_paused)
        self.defender.executor.execute.assert_not_awaited()

    async def test_cancelled_attack_during_inventory_read_blocks(self):
        self.defender.reader.capture.side_effect = [Document(defense_html(self.flight)), Document(defense_html(self.flight, clear=True))]
        report = await self.defender.process()
        self.assertEqual(report["result"]["state"], "blocked")
        self.defender.executor.execute.assert_not_awaited()

    async def test_pending_fleet_is_reconciled_without_a_second_escape(self):
        self.journal.prepare(self.flight)
        await self.defender.process()
        self.defender.executor.reconcile.assert_awaited_once()
        self.defender.executor.execute.assert_not_awaited()

    async def test_clear_reading_releases_pause(self):
        self.session.defense_paused = True
        self.defender.reader.capture.side_effect = lambda *args, **kwargs: Document(defense_html(self.flight, clear=True))
        report = await self.defender.process()
        self.assertEqual(report["state"], "clear")
        self.assertFalse(self.session.defense_paused)

    async def test_monitor_failure_stops_the_ordinary_loop(self):
        from ogame_bot.app import run
        self.session.connect = AsyncMock()
        self.session.state = SessionState.READY
        self.session.before_navigation = None
        browser = MagicMock()
        browser.__aenter__ = AsyncMock(return_value=browser)
        browser.__aexit__ = AsyncMock(return_value=False)
        monitor = MagicMock()
        monitor.process = AsyncMock(side_effect=CoreError("Synthetic monitor failure"))
        monitor.last_state = "unknown"
        notifier = MagicMock()
        notifier.close = AsyncMock()
        scanner = MagicMock()
        scanner.scan = AsyncMock()
        cfg = replace(self.config, brain=replace(self.config.brain, journal_file=Path(self.temp.name) / "runtime.sqlite3"), scheduler=replace(self.config.scheduler, status_file=Path(self.temp.name) / "scheduler.json"))
        with patch("ogame_bot.app.BrowserManager", return_value=browser), patch("ogame_bot.app.SessionManager", return_value=self.session), patch("ogame_bot.app.Defender", return_value=monitor), patch("ogame_bot.app.AlertDispatcher", return_value=notifier), patch("ogame_bot.scheduler.runtime.Scanner", return_value=scanner):
            with self.assertRaises(CoreError):
                await run(cfg)
        scanner.scan.assert_not_awaited()
        notifier.close.assert_awaited_once()
        self.assertIsNone(self.session.before_navigation)
        self.assertEqual(json.loads(cfg.scheduler.status_file.read_text())["state"], "failed")


if __name__ == "__main__":
    unittest.main()
