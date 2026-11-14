"""Planificación, registro previo, ejecución única y recuperación sin cuenta real."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml
from playwright.async_api import Error

from ogame_bot.brain.catalog import BUILD, RESEARCH
from ogame_bot.brain.planner import Planner, Upgrade
from ogame_bot.brain.service import Brain
from ogame_bot.brain.settings import Goal
from ogame_bot.config import load_config
from ogame_bot.core.action_executor import ActionExecutor
from ogame_bot.domain.models import AccountSnapshot, BodySnapshot, CelestialBody, Reading, utc_now
from ogame_bot.errors import ConfigurationError, CoreError
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.parsing import queue_state
from ogame_bot.storage.orders import OrderJournal

ROOT = Path(__file__).resolve().parents[1]


def observed(data):
    return Reading(data, utc_now(), "complete")


def snapshot() -> AccountSnapshot:
    account = AccountSnapshot("Test Commander", "https://s277-es.ogame.gameforge.com", utc_now())
    body = BodySnapshot(CelestialBody(101, "1:200:8", "planet", "Prueba"))
    body.resources = observed({"metal": 1000000, "crystal": 1000000, "deuterium": 1000000, "energy": 10000})
    body.buildings = observed({"metal_mine": 3, "crystal_mine": 2, "deuterium_synthesizer": 1, "solar_plant": 5})
    body.facilities = observed({"research_lab": 3})
    body.construction_queue = observed({"state": "free"})
    account.bodies = [body]
    account.movements = observed({"threat_status": "clear", "events": []})
    account.research = observed({"energy_technology": 0, "astrophysics": 1, "espionage_technology": 3, "plasma_technology": 0})
    account.research_queue = observed({"state": "free"})
    return account


def document(level: int = 3, *, busy: bool = False, energy: int = 10000, player: str = "Test Commander", research: bool = False) -> str:
    technology = RESEARCH["energy_technology"] if research else BUILD["metal_mine"]
    empty = "No hay ninguna investigación en progreso en este momento." if research else "No hay edificios en construcción"
    queue = '<span class="countDown">10</span>' if busy else empty
    return f'''<html><head><meta name="ogame-planet-id" content="101"></head><body>
    <span id="playerName">{player}</span>
    <div class="smallplanet"><a class="planetlink" href="/game/index.php?cp=101"><span class="planet-name">Prueba</span><span class="planet-koords">[1:200:8]</span></a></div>
    <span id="resources_metal">1000000</span><span id="resources_crystal">1000000</span>
    <span id="resources_deuterium">1000000</span><span id="resources_energy">{energy}</span>
    <div id="productionboxBottom">{queue}</div><div id="attack_alert" data-status="noAttack"></div>
    <li class="technology" data-technology="{technology.id}" aria-label="{technology.name_es}"><span class="level">{level}</span></li>
    </body></html>'''


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "config.example.yaml")
        self.settings = replace(self.config.brain, research_enabled=False)

    def test_priority_advances_only_one_level(self) -> None:
        actions = Planner(self.settings, 120).plan(snapshot())
        self.assertEqual(len(actions), 1)
        self.assertEqual((actions[0].item, actions[0].current_level, actions[0].target_level), ("metal_mine", 3, 4))

    def test_completed_goal_advances_to_next(self) -> None:
        account = snapshot()
        account.bodies[0].buildings.data["metal_mine"] = 30
        action = Planner(self.settings, 120).plan(account)[0]
        self.assertEqual(action.item, "crystal_mine")

    def test_unknown_first_priority_does_not_skip(self) -> None:
        account = snapshot()
        del account.bodies[0].buildings.data["metal_mine"]
        planner = Planner(self.settings, 120)
        self.assertEqual(planner.plan(account), [])
        self.assertIn("priority_level_unknown_metal_mine", [note["reason"] for note in planner.notes])

    def test_energy_deficit_preempts_mines(self) -> None:
        account = snapshot()
        account.bodies[0].resources.data["energy"] = -1
        action = Planner(self.settings, 120).plan(account)[0]
        self.assertEqual((action.item, action.reason), ("solar_plant", "energy_deficit"))

    def test_next_mine_energy_is_checked_before_upgrade(self) -> None:
        account = snapshot()
        account.bodies[0].resources.data["energy"] = 0
        action = Planner(self.settings, 120).plan(account)[0]
        self.assertEqual(action.item, "solar_plant")
        self.assertEqual(action.reason, "energy_for_metal_mine")

    def test_reserve_is_not_spent(self) -> None:
        account = snapshot()
        account.bodies[0].resources.data["deuterium"] = 99999
        self.assertEqual(Planner(self.settings, 120).plan(account), [])

    def test_busy_unknown_or_stale_queue_blocks(self) -> None:
        for reading in [observed({"state": "busy"}), Reading.unavailable("test"), Reading({"state": "free"}, "2000-01-01T00:00:00+00:00", "complete")]:
            with self.subTest(reading=reading):
                account = snapshot()
                account.bodies[0].construction_queue = reading
                self.assertEqual(Planner(self.settings, 120).plan(account), [])

    def test_unknown_or_detected_or_stale_attack_status_blocks(self) -> None:
        for reading in [observed({"threat_status": "unknown"}), observed({"threat_status": "detected"}), Reading({"threat_status": "clear"}, "2000-01-01T00:00:00+00:00", "complete")]:
            account = snapshot()
            account.movements = reading
            self.assertEqual(Planner(self.settings, 120).plan(account), [])

    def test_stale_resources_block(self) -> None:
        account = snapshot()
        account.bodies[0].resources = Reading(account.bodies[0].resources.data, "2000-01-01T00:00:00+00:00", "complete")
        self.assertEqual(Planner(self.settings, 120).plan(account), [])

    def test_roi_selects_lowest_modeled_payback(self) -> None:
        settings = replace(self.settings, strategy="roi", economy_speed=8, temperatures={101: 40}, max_payback_days=10000)
        action = Planner(settings, 120).plan(snapshot())[0]
        self.assertIn(action.item, {"metal_mine", "crystal_mine", "deuterium_synthesizer"})
        self.assertGreater(action.payback_days, 0)
        # Con un peso de deuterio muy grande, gana el sintetizador de nivel bajo.
        settings = replace(settings, resource_weights={"metal": 1.0, "crystal": 1.0, "deuterium": 1000.0})
        self.assertEqual(Planner(settings, 120).plan(snapshot())[0].item, "deuterium_synthesizer")

    def test_roi_requires_temperature_for_configured_deuterium(self) -> None:
        settings = replace(self.settings, strategy="roi", economy_speed=8)
        planner = Planner(settings, 120)
        actions = planner.plan(snapshot())
        self.assertIn(actions[0].item, {"metal_mine", "crystal_mine"})
        self.assertIn("roi_temperature_unknown", [note["reason"] for note in planner.notes])

    def test_roi_includes_solar_in_payback(self) -> None:
        settings = replace(self.settings, strategy="roi", economy_speed=8, temperatures={101: 40}, build_goals=(Goal("metal_mine", 30),), max_payback_days=10000)
        account = snapshot()
        ample = Planner(settings, 120).plan(account)[0]
        account.bodies[0].resources.data["energy"] = 0
        limited = Planner(settings, 120).plan(account)[0]
        self.assertEqual(limited.item, "solar_plant")
        self.assertGreater(limited.payback_days, ample.payback_days)

    def test_research_priority_uses_global_queue(self) -> None:
        settings = replace(self.settings, builder_enabled=False, research_enabled=True, research_goals=(Goal("energy_technology", 3),))
        account = snapshot()
        action = Planner(settings, 120).plan(account)[0]
        self.assertEqual((action.kind, action.item, action.target_level), ("research", "energy_technology", 1))
        account.research_queue = observed({"state": "busy"})
        self.assertEqual(Planner(settings, 120).plan(account), [])


class SettingsAndQueueTests(unittest.TestCase):
    def test_invalid_brain_settings_are_rejected(self) -> None:
        for case in ("missing_speed", "unknown_technology", "duplicate_goal", "colliding_paths", "negative_reserve"):
            raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
            if case == "missing_speed":
                raw["builder"]["strategy"] = "roi"
            elif case == "unknown_technology":
                raw["builder"]["priorities"][0]["item"] = "nonexistent"
            elif case == "duplicate_goal":
                raw["builder"]["priorities"].append(raw["builder"]["priorities"][0])
            elif case == "colliding_paths":
                raw["automation"]["journal_file"] = raw["automation"]["plan_file"]
            else:
                raw["builder"]["reserve"]["metal"] = -1
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(yaml.safe_dump(raw), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_config(path)

    def test_queue_needs_explicit_scoped_evidence(self) -> None:
        self.assertEqual(queue_state(Document(document())).data["state"], "free")
        self.assertEqual(queue_state(Document(document(busy=True))).data["state"], "busy")
        self.assertIsNone(queue_state(Document("<p>No hay edificios en construcción</p>")).data)
        self.assertIsNone(queue_state(Document('<div id="productionboxBottom">Cargando</div>')).data)

    def test_classic_costs_and_astrophysics_upper_rounding(self) -> None:
        self.assertEqual(BUILD["metal_mine"].cost(4), {"metal": 202, "crystal": 50, "deuterium": 0})
        self.assertEqual(RESEARCH["energy_technology"].cost(1), {"metal": 0, "crystal": 800, "deuterium": 400})
        self.assertEqual(RESEARCH["astrophysics"].cost(4), {"metal": 21500, "crystal": 42900, "deuterium": 21500})


class JournalTests(unittest.TestCase):
    def test_pending_order_survives_restart_and_blocks_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            action = Planner(replace(load_config(ROOT / "config.example.yaml").brain, research_enabled=False), 120).plan(snapshot())[0]
            journal = OrderJournal(path, "test-account")
            identifier = journal.prepare(action)
            journal.close()
            journal = OrderJournal(path, "test-account")
            try:
                self.assertEqual(journal.pending()[0], identifier)
                with self.assertRaises(CoreError):
                    journal.prepare(action)
                journal.mark(identifier, "confirmed")
                self.assertIsNone(journal.pending())
                self.assertEqual(journal.last_completed_kind(), "building")
            finally:
                journal.close()

    def test_journal_cannot_be_reused_for_another_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            OrderJournal(path, "account-a").close()
            with self.assertRaises(CoreError):
                OrderJournal(path, "account-b")


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = load_config(ROOT / "config.example.yaml")
        settings = replace(config.brain, dry_run=False, journal_file=Path(self.temp.name) / "orders.sqlite3", plan_file=Path(self.temp.name) / "plan.json", research_enabled=False)
        self.config = replace(config, brain=settings)
        self.journal = OrderJournal(settings.journal_file, "test-account")
        self.addCleanup(self.journal.close)
        self.action = Planner(settings, 120).plan(snapshot())[0]
        self.page = MagicMock()
        self.html = document()
        self.page.content = AsyncMock(side_effect=lambda: self.html)
        self.button = MagicMock()
        self.button.count = AsyncMock(return_value=1)
        self.button.is_visible = AsyncMock(return_value=True)
        self.button.is_enabled = AsyncMock(return_value=True)
        self.button.click = AsyncMock()
        self.card = MagicMock()
        self.card.count = AsyncMock(return_value=1)
        self.card.get_attribute = AsyncMock(return_value="Mina de metal")
        self.card.get_by_role.return_value = self.button
        self.card.first.wait_for = AsyncMock()
        self.page.locator.return_value = self.card
        self.session = MagicMock()

        @asynccontextmanager
        async def read_page(*args, **kwargs):
            yield self.page

        self.session.read_page = read_page
        self.executor = ActionExecutor(self.session, self.config, self.journal)

    async def test_record_is_committed_before_click(self) -> None:
        async def click(**kwargs):
            self.assertIsNotNone(self.journal.pending())
        self.button.click.side_effect = click
        result = await self.executor.execute(self.action)
        self.assertEqual(result["state"], "pending")
        self.button.click.assert_awaited_once()

    async def test_pending_order_is_never_clicked_twice(self) -> None:
        await self.executor.execute(self.action)
        result = await self.executor.execute(self.action)
        self.assertEqual(result["state"], "pending")
        self.button.click.assert_awaited_once()

    async def test_ambiguous_click_stays_blocked(self) -> None:
        self.button.click.side_effect = Error("Synthetic navigation failure after send")
        with self.assertRaises(Error):
            await self.executor.execute(self.action)
        self.assertIsNotNone(self.journal.pending())
        await self.executor.reconcile()
        self.button.click.assert_awaited_once()

    async def test_completed_level_reconciles_without_click(self) -> None:
        self.journal.prepare(self.action)
        self.html = document(level=4)
        result = await self.executor.reconcile()
        self.assertEqual(result["state"], "confirmed")
        self.assertIsNone(self.journal.pending())
        self.button.click.assert_not_awaited()

    async def test_changed_energy_or_queue_blocks_before_record(self) -> None:
        for html in (document(busy=True), document(energy=0), document(level=4)):
            self.html = html
            result = await self.executor.execute(self.action)
            self.assertEqual(result["state"], "blocked")
            self.assertIsNone(self.journal.pending())
        self.button.click.assert_not_awaited()

    async def test_dry_run_never_records_or_clicks(self) -> None:
        config = replace(self.config, brain=replace(self.config.brain, dry_run=True))
        result = await ActionExecutor(self.session, config, self.journal).execute(self.action)
        self.assertEqual(result["state"], "simulated")
        self.assertIsNone(self.journal.pending())
        self.button.click.assert_not_awaited()

    async def test_disabled_button_does_not_record_an_order(self) -> None:
        self.button.is_enabled.return_value = False
        result = await self.executor.execute(self.action)
        self.assertEqual(result["state"], "blocked")
        self.assertIsNone(self.journal.pending())

    async def test_research_uses_exact_button_and_same_journal(self) -> None:
        self.html = document(level=0, research=True)
        self.card.get_attribute.return_value = "Tecnología de energía"
        config = replace(self.config, brain=replace(self.config.brain, research_enabled=True, research_goals=(Goal("energy_technology", 3),)))
        action = Upgrade("research", "energy_technology", 101, "1:200:8", 0, 1, RESEARCH["energy_technology"].cost(1), "test")
        result = await ActionExecutor(self.session, config, self.journal).execute(action)
        self.assertEqual(result["state"], "pending")
        self.card.get_by_role.assert_called_once_with("button", name="Investigar Tecnología de energía del nivel 1", exact=True)

    async def test_disabled_module_and_unconfigured_goal_cannot_execute(self) -> None:
        config = replace(self.config, brain=replace(self.config.brain, builder_enabled=False))
        result = await ActionExecutor(self.session, config, self.journal).execute(self.action)
        self.assertEqual(result["state"], "blocked")
        action = Upgrade("research", "energy_technology", 101, "1:200:8", 0, 1, RESEARCH["energy_technology"].cost(1), "test")
        result = await self.executor.execute(action)
        self.assertEqual(result["state"], "blocked")
        self.assertIsNone(self.journal.pending())
        self.button.click.assert_not_awaited()

    async def test_brain_writes_report_in_simulation(self) -> None:
        config = replace(self.config, brain=replace(self.config.brain, dry_run=True))
        executor = ActionExecutor(self.session, config, self.journal)
        report = await Brain(config, executor, self.journal).process(snapshot())
        saved = json.loads(config.brain.plan_file.read_text(encoding="utf-8"))
        self.assertEqual(report["result"]["state"], "simulated")
        self.assertEqual(saved["result"]["state"], "simulated")
        self.assertEqual(saved["selected"]["item"], "metal_mine")
        self.button.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
