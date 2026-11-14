"""Secuencia acotada y fabricación única de la sonda sin acceso al juego."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import Error

from ogame_bot.brain.catalog import BUILD, RESEARCH, SHIP_BUILD
from ogame_bot.brain.planner import Planner, Upgrade
from ogame_bot.brain.service import Brain
from ogame_bot.brain.settings import Goal
from ogame_bot.config import load_config
from ogame_bot.core.action_executor import ActionExecutor
from ogame_bot.errors import ConfigurationError
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.parsing import queue_state
from ogame_bot.storage.orders import OrderJournal
from test_brain import observed, snapshot


ROOT = Path(__file__).resolve().parents[1]


def configured():
    cfg = load_config(ROOT / "config.example.yaml")
    return replace(cfg, brain=replace(cfg.brain, dry_run=True, probe_target=1, probe_body_id=101, probe_bootstrap_live=True,
                                     reserve={"metal": 0, "crystal": 0, "deuterium": 100}))


def prepared_state():
    state = snapshot()
    state.bodies[0].facilities = observed({"robotics_factory": 3, "shipyard": 3, "research_lab": 3})
    state.bodies[0].ships = observed({"espionage_probe": 0})
    state.research = observed({"energy_technology": 2, "combustion_drive": 2, "espionage_technology": 1})
    return state


def probe_order() -> Upgrade:
    return Upgrade("shipyard", "espionage_probe", 101, "1:200:8", 0, 1, SHIP_BUILD["espionage_probe"].cost(1), "probe_bootstrap")


class BootstrapPlanningTests(unittest.TestCase):
    def test_sequence_then_one_probe_then_no_more_actions(self):
        cfg, state = configured(), prepared_state()
        planner = Planner(cfg.brain, 120)
        self.assertEqual(planner.plan(state)[0].item, "combustion_drive")
        state.research.data["combustion_drive"] = 3
        self.assertEqual(planner.plan(state)[0].item, "espionage_technology")
        state.research.data["espionage_technology"] = 2
        self.assertEqual(planner.plan(state), [probe_order()])
        state.bodies[0].ships.data["espionage_probe"] = 1
        self.assertEqual(planner.plan(state), [])

    def test_busy_research_and_attacks_prevent_progress(self):
        cfg, state = configured(), prepared_state()
        planner = Planner(cfg.brain, 120)
        state.research_queue = observed({"state": "busy"})
        self.assertEqual(planner.plan(state), [])
        state.research_queue = observed({"state": "free"})
        state.movements.data["threat_status"] = "detected"
        self.assertEqual(planner.plan(state), [])

    def test_missing_facility_data_is_not_guessed(self):
        state = prepared_state()
        state.bodies[0].facilities.data.pop("shipyard")
        self.assertEqual(Planner(configured().brain, 120).plan(state), [])

    def test_sonda_quantity_cannot_be_two(self):
        action = replace(probe_order(), current_level=1, target_level=2)
        with self.assertRaises(ValueError):
            Upgrade.from_dict(action.to_dict())

    def test_real_shipyard_idle_markup(self):
        doc = Document('<div id="productionboxshipyardcomponent"><h3>Hangar</h3><td class="idle"><a>No se están construyendo naves o defensas.<br>(Al Hangar)</a></td></div>')
        self.assertEqual(queue_state(doc, shipyard=True).data, {"state": "free"})
        self.assertIsNone(queue_state(Document('<div id="productionboxshipyardcomponent">Cargando</div>'), shipyard=True).data)

    def test_configuration_requires_explicit_origin_and_single_target(self):
        import yaml
        raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            for extra in ({"probe_target": 2}, {"probe_target": 1}, {"probe_bootstrap_live": True}):
                candidate = {**raw, "automation": {**raw["automation"], **extra}}
                path.write_text(yaml.safe_dump(candidate), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_config(path)


class BootstrapExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = configured()
        self.config = replace(cfg, brain=replace(cfg.brain, plan_file=Path(self.temp.name) / "plan.json"))
        self.journal = OrderJournal(Path(self.temp.name) / "orders.sqlite3", "test")
        self.addCleanup(self.journal.close)
        self.built = False
        self.quantity = "0"
        self.page = MagicMock()
        self.page.content = AsyncMock(side_effect=lambda: self.html())
        self.general = MagicMock()
        self.general.first.wait_for = AsyncMock()
        self.card = MagicMock(count=AsyncMock(return_value=1), click=AsyncMock())
        self.card.get_attribute = AsyncMock(side_effect=lambda name: {"aria-label": "Sonda de espionaje", "data-status": "on"}.get(name))
        self.amount = MagicMock(count=AsyncMock(return_value=1), wait_for=AsyncMock())
        async def fill(value):
            self.quantity = value
        self.amount.fill = AsyncMock(side_effect=fill)
        self.amount.input_value = AsyncMock(side_effect=lambda: self.quantity)
        self.sender = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), is_enabled=AsyncMock(return_value=True))
        async def send(**kwargs):
            self.assertEqual(self.quantity, "1")
            self.assertIsNotNone(self.journal.pending("upgrade"))
            self.built = True
        self.sender.click = AsyncMock(side_effect=send)
        controls = {'.technology[data-technology="210"]': self.card,
                    '#technologydetails #build_amount': self.amount,
                    '#technologydetails button.upgrade[data-technology="210"]': self.sender}
        self.page.locator.side_effect = lambda selector: controls.get(selector, self.general)
        self.session = MagicMock()
        self.session.defense_paused = False
        @asynccontextmanager
        async def read_page(*args, **kwargs):
            yield self.page
        self.session.read_page = read_page
        self.executor = ActionExecutor(self.session, self.config, self.journal)

    def html(self):
        return f'''<span id="playerName">{self.config.expected_player_name}</span><meta name="ogame-planet-id" content="101">
        <div class="smallplanet"><a class="planetlink" href="/game/index.php?cp=101"><span class="planet-koords">[1:200:8]</span></a></div>
        <div id="resources_metal">10000</div><div id="resources_crystal">10000</div><div id="resources_deuterium">10000</div><div id="resources_energy">0</div>
        <div id="attack_alert" class="noAttack"></div>
        <li class="technology" data-technology="210"><span class="amount">{int(self.built)}</span></li>
        <div id="productionboxshipyardcomponent"><td class="idle">No se están construyendo naves o defensas.</td></div>'''

    async def test_single_order_then_reconcile_and_never_rebuild_after_departure(self):
        result = await self.executor.execute(probe_order())
        self.assertEqual(result["state"], "pending")
        self.assertTrue(self.config.brain.dry_run)
        self.assertEqual((await self.executor.reconcile())["state"], "confirmed")
        self.assertTrue(self.journal.probe_completed(101))
        self.built = False
        report = await Brain(self.config, self.executor, self.journal).process(prepared_state())
        self.assertTrue(report["probe_bootstrap_completed"])
        self.assertEqual(report["automation_mode"], "routine")
        self.assertFalse(any(action["kind"] == "shipyard" for action in report["actions"]))
        self.sender.click.assert_awaited_once()

    async def test_completed_probe_resumes_saved_mine_goals_in_simulation(self):
        identifier = self.journal.prepare(probe_order())
        self.journal.mark(identifier, "confirmed")
        cfg = replace(self.config, brain=replace(self.config.brain, build_goals=(Goal("metal_mine", 16),), research_enabled=False, economic_live=False))
        executor = ActionExecutor(self.session, cfg, self.journal)
        report = await Brain(cfg, executor, self.journal).process(prepared_state())
        self.assertEqual(report["selected"]["item"], "metal_mine")
        self.assertEqual(report["result"]["state"], "simulated")
        self.assertTrue(report["dry_run"])
        self.sender.click.assert_not_awaited()

    async def test_completed_probe_allows_only_configured_live_routine_goals(self):
        identifier = self.journal.prepare(probe_order())
        self.journal.mark(identifier, "confirmed")
        cfg = replace(self.config, brain=replace(self.config.brain, build_goals=(Goal("metal_mine", 16),), research_enabled=False, economic_live=True))
        from test_brain import document
        self.page.content = AsyncMock(return_value=document(level=3))
        self.card.get_attribute = AsyncMock(return_value="Mina de metal")
        self.card.get_by_role.return_value = self.sender
        self.page.locator.side_effect = lambda selector: self.card if selector == '.technology[data-technology="1"]' else self.general
        self.sender.click = AsyncMock()
        report = await Brain(cfg, ActionExecutor(self.session, cfg, self.journal), self.journal).process(prepared_state())
        self.assertEqual(report["selected"]["item"], "metal_mine")
        self.assertFalse(report["dry_run"])
        self.sender.click.assert_awaited_once()
        self.assertEqual((await ActionExecutor(self.session, cfg, self.journal).execute(probe_order()))["state"], "pending")
        self.sender.click.assert_awaited_once()

    async def test_uncertain_build_is_not_clicked_again(self):
        self.sender.click.side_effect = Error("synthetic send uncertainty")
        with self.assertRaises(Error):
            await self.executor.execute(probe_order())
        self.assertIsNotNone(self.journal.pending("upgrade"))
        await self.executor.execute(probe_order())
        self.sender.click.assert_awaited_once()

    async def test_live_exception_does_not_allow_mines_or_advanced_research(self):
        actions = [Upgrade("building", "metal_mine", 101, "1:200:8", 3, 4, BUILD["metal_mine"].cost(4), "test"),
                   Upgrade("research", "combustion_drive", 101, "1:200:8", 3, 4, RESEARCH["combustion_drive"].cost(4), "test")]
        for action in actions:
            self.assertEqual((await self.executor.execute(action))["state"], "blocked")
        self.sender.click.assert_not_awaited()

    async def test_default_flag_keeps_preparation_simulated(self):
        cfg = replace(self.config, brain=replace(self.config.brain, probe_bootstrap_live=False))
        self.assertEqual((await ActionExecutor(self.session, cfg, self.journal).execute(probe_order()))["state"], "simulated")
        self.sender.click.assert_not_awaited()
