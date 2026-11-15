"""Quiet periods, explicit completion times and lightweight economy reads."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ogame_bot.config import load_config
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.parsing import queue_state
from ogame_bot.scanner.service import Scanner
from ogame_bot.scheduler.runtime import BotRuntime
from ogame_bot.scheduler.waiting import next_economic_check
from ogame_bot.core.session import SessionState
from test_brain import snapshot, observed
from ogame_bot.brain.planner import Upgrade, Planner
from ogame_bot.brain.settings import Goal
from ogame_bot.brain.catalog import BUILD
from test_scanner import HTML

ROOT = Path(__file__).resolve().parents[1]


class QueueTimerTests(unittest.TestCase):
    def test_lowercase_countdown_and_explicit_completion_are_preserved(self):
        end = int((datetime.now(timezone.utc) + timedelta(seconds=300)).timestamp())
        doc = Document(f'<div id="productionboxBottom"><div id="productionboxbuildingcomponent"><span class="countdown" data-end="{end}">00:05:00</span></div></div>')
        queue = queue_state(doc)
        self.assertEqual(queue.data['state'], 'busy')
        self.assertIn('ends_at', queue.data)

    def test_unknown_completion_does_not_create_an_eta(self):
        doc = Document('<div id="productionboxBottom"><span class="countDown">Cargando</span></div>')
        self.assertEqual(queue_state(doc).data, {'state': 'busy'})


class QuietRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cfg = load_config(ROOT / 'config.example.yaml')
        self.config = replace(cfg, fleet=replace(cfg.fleet, enabled=False), scheduler=replace(cfg.scheduler, status_file=Path(self.temp.name)/'scheduler.json'), brain=replace(cfg.brain, plan_file=Path(self.temp.name)/'plan.json'))
        self.session = MagicMock()
        self.session.state = SessionState.READY
        self.session.before_navigation = None
        self.session.observe = AsyncMock()
        self.session.check = AsyncMock()
        self.journal = MagicMock()
        self.journal.pending.return_value = None
        self.journal.pending_upgrades.return_value = []
        self.runtime = BotRuntime(self.config, self.session, self.journal, None)
        self.runtime.scanner.scan = AsyncMock(return_value=snapshot())
        self.runtime.scanner.refresh_economy = AsyncMock(return_value=snapshot())
        self.runtime.brain.process = AsyncMock(return_value={'result': {'state':'no_action'}, 'notes': []})

    async def test_idle_plan_does_not_run_scanner_or_brain_again_every_minute(self):
        clock = [0.0]
        self.runtime.scheduler.clock = lambda: clock[0]
        for job in self.runtime.scheduler.jobs.values():
            job.next_due = 0
        await self.runtime.scheduler.run_due()
        self.assertEqual(self.runtime._scanner_ready(), 'waiting_for_event')
        brain_due = self.runtime.scheduler.jobs['brain'].next_due
        self.assertEqual(brain_due, 600)
        for instant in (3,30,60,90,300,599):
            clock[0] = instant
            await self.runtime.scheduler.run_due()
        self.runtime.scanner.scan.assert_awaited_once()
        self.runtime.brain.process.assert_awaited_once()
        self.runtime.scanner.refresh_economy.assert_not_awaited()
        clock[0] = 600
        await self.runtime.scheduler.run_due()
        self.runtime.scanner.refresh_economy.assert_awaited_once()

    async def test_pending_upgrade_defers_confirmation_and_blocks_other_upgrade_clicks(self):
        self.runtime.latest = snapshot()
        self.runtime._snapshot_finished = self.runtime.scheduler.clock()
        self.runtime.brain.process.return_value = {'result': {'state':'pending'}, 'notes': []}
        self.journal.pending.return_value = ('order', MagicMock())
        await self.runtime._brain()
        self.assertIsNone(self.runtime._brain_ready())

    async def test_session_job_observes_without_refreshing(self):
        await self.runtime._session()
        self.session.observe.assert_awaited_once()
        self.session.check.assert_not_awaited()

    async def test_unrelated_settings_keep_existing_module_deadlines(self):
        before = {name:job.next_due for name,job in self.runtime.scheduler.jobs.items()}
        self.runtime._configure_jobs(set())
        self.assertEqual(before, {name:job.next_due for name,job in self.runtime.scheduler.jobs.items()})

    async def test_finished_upgrade_is_confirmed_from_scan_without_another_navigation(self):
        state = snapshot()
        state.bodies[0].buildings.data['solar_plant'] = 6
        self.runtime.latest = state
        action = Upgrade('building','solar_plant',101,'1:200:8',5,6,BUILD['solar_plant'].cost(6),'energy_deficit')
        self.journal.pending_upgrades.return_value = [('order',action)]
        self.runtime._confirm_snapshot_upgrade()
        self.journal.mark.assert_called_once_with('order','confirmed')


class EconomyReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_reads_fields_supplies_and_research_without_fleet_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = replace(load_config(ROOT/'config.example.yaml'), scanner_output_file=Path(folder)/'state.json')
            reader = MagicMock(capture=AsyncMock(return_value=Document(HTML)))
            await Scanner(reader,cfg).refresh_economy(snapshot())
            self.assertEqual([call.args[0] for call in reader.capture.await_args_list], ['overview','supplies','research'])


class WakeupTests(unittest.TestCase):
    def test_research_is_not_misreported_as_unaffordable_when_building_queue_is_busy(self):
        cfg = load_config(ROOT/'config.example.yaml')
        settings = replace(cfg.brain,builder_enabled=False,research_enabled=True,research_goals=(Goal('impulse_drive',2),))
        state = snapshot()
        state.research = observed({'impulse_drive':1})
        state.bodies[0].construction_queue = observed({'state':'busy'})
        planner = Planner(settings,120)
        actions = planner.plan(state)
        self.assertEqual([action.item for action in actions],['impulse_drive'])
        self.assertFalse(any(note['reason']=='research_resources_or_reserve_insufficient' for note in planner.notes))

    def test_explicit_receipt_completion_overrides_short_polling_intervals(self):
        cfg = load_config(ROOT/'config.example.yaml')
        report = {'result': {'state':'pending','retry_at': (datetime.now(timezone.utc)+timedelta(seconds=240)).isoformat()}}
        delay, reason = next_economic_check(cfg,snapshot(),report)
        self.assertTrue(240 <= delay <= 246)
        self.assertEqual(reason,'queue_finishes')

    def test_no_production_data_uses_honest_reserve_interval(self):
        cfg = load_config(ROOT/'config.example.yaml')
        self.assertEqual(next_economic_check(cfg,snapshot(),{'result':{'state':'pending'}}),(600,'completion_time_unknown'))
