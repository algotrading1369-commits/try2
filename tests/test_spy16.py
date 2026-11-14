"""Evacuación por espionaje al espacio profundo, sin envíos reales."""
from __future__ import annotations

import unittest
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import test_defender as fixtures
from ogame_bot.defender.planner import EscapePlanner, destination_options
from ogame_bot.config import Config, load_config
from ogame_bot.errors import ConfigurationError
from ogame_bot.defender.threats import Threat, analyze, escape_guard, escape_return_guard
from ogame_bot.domain.models import AccountSnapshot, Reading
from ogame_bot.fleet.executor import FleetExecutor
from ogame_bot.fleet.models import Flight
from ogame_bot.scanner.dom import Document


def spy_plan() -> tuple[Config, AccountSnapshot, Threat, EscapePlanner]:
    cfg = fixtures.config()
    cfg = replace(cfg, defender=replace(cfg.defender, mission=6, destinations=(), minimum_flight_seconds=60))
    state = fixtures.threatened()
    state.registry.data[:] = [state.bodies[0].body]
    state.fleet_slots.data.update(expedition_total=0, expedition_used=0)
    state.research = Reading.unavailable("not_needed_for_spy16")
    _, threats = analyze(state.movements, state.registry, cfg.defender)
    planner = EscapePlanner(cfg.defender, cfg.fleet.capacities, cfg.brain.resource_weights)
    return cfg, state, threats[0], planner


class SpyPlanningTests(unittest.TestCase):
    def test_yaml_option_and_minimum_speed(self) -> None:
        example = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(example.replace("mission: deployment", "mission: espionage16"), encoding="utf-8")
            self.assertEqual(load_config(path).defender.mission, 6)
            path.write_text(path.read_text(encoding="utf-8").replace("  speed_percent: 10", "  speed_percent: 20"), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_one_planet_no_astrophysics_or_expedition_slots(self) -> None:
        cfg, state, threat, planner = spy_plan()
        flight = planner.plan(state, threat)
        self.assertIsNotNone(flight)
        self.assertEqual((flight.mission, flight.destination, flight.destination_id, flight.speed_percent), (6, "1:200:16", None, 10))
        self.assertGreater(sum(flight.resources.values()), 0)
        self.assertEqual(destination_options(state, 101, cfg.defender), ([], None))
        self.assertGreaterEqual(2 * flight.minimum_duration_seconds, threat.seconds_to_impact + cfg.defender.minimum_lead_seconds)
        self.assertEqual(Flight.from_dict(flight.to_dict()), flight)

    def test_probe_is_required(self) -> None:
        _, state, threat, planner = spy_plan()
        state.bodies[0].ships.data["espionage_probe"] = 0
        self.assertIsNone(planner.plan(state, threat))
        self.assertEqual(planner.reason, "espionage_probe_required")

    def test_cargo_is_required(self) -> None:
        _, state, threat, planner = spy_plan()
        state.bodies[0].ships.data["large_cargo"] = 0
        self.assertIsNone(planner.plan(state, threat))
        self.assertEqual(planner.reason, "cargo_ship_required")

    def test_spy_mission_cannot_target_players_or_run_as_routine(self) -> None:
        _, state, threat, planner = spy_plan()
        flight = planner.plan(state, threat)
        for changes in ({"destination": "1:200:8"}, {"destination": "1:201:16"}, {"purpose": "routine"}, {"speed_percent": 20}, {"destination_id": 102}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(flight, **changes).validate()

    def test_slot16_does_not_confuse_absent_destination_ids_with_attack(self) -> None:
        cfg, state, threat, planner = spy_plan()
        flight = planner.plan(state, threat)
        html = fixtures.defense_html(flight).replace('data-destination-id="101"', '')
        self.assertIsNone(escape_guard(Document(html), flight, cfg.defender))

    def test_return_must_be_after_attack_with_margin(self) -> None:
        cfg, state, threat, planner = spy_plan()
        flight = planner.plan(state, threat)
        now = datetime.now(timezone.utc)
        flight = replace(flight, impact_at=(now + timedelta(seconds=300)).isoformat())
        self.assertEqual(escape_return_guard(flight, 100, cfg.defender, now), "return_before_attack_margin")
        self.assertEqual(escape_return_guard(flight, 165, cfg.defender, now), "return_before_attack_margin")
        self.assertIsNone(escape_return_guard(flight, 180, cfg.defender, now))


class SpySimulationTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulation_requires_live_quote_and_never_opens_form(self) -> None:
        cfg, state, threat, planner = spy_plan()
        flight = planner.plan(state, threat)
        cfg = replace(cfg, brain=replace(cfg.brain, dry_run=True))
        session, journal = MagicMock(), MagicMock()
        journal.pending.return_value = None
        result = await FleetExecutor(session, cfg, journal).execute(flight)
        self.assertEqual(result["state"], "simulated")
        self.assertFalse(result["timing_verified"])
        session.read_page.assert_not_called()
        journal.prepare.assert_not_called()
