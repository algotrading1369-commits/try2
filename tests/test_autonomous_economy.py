"""Stateful economy: real planner, executor, SQLite journal and scheduler."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ogame_bot.brain.catalog import BUILD, RESEARCH
from ogame_bot.brain.planner import Upgrade
from ogame_bot.brain.settings import Goal
from ogame_bot.config import load_config
from ogame_bot.core.session import SessionState
from ogame_bot.domain.models import utc_now
from ogame_bot.scheduler.runtime import BotRuntime
from ogame_bot.storage.orders import OrderJournal
from test_brain import observed, snapshot

ROOT = Path(__file__).resolve().parents[1]


class AutonomousEconomyTests(unittest.IsolatedAsyncioTestCase):
    async def run_chain(self, strategy: str) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            base = Path(folder)
            config = load_config(ROOT / 'config.example.yaml')
            config = replace(config, scanner_output_file=base/'state.json', fleet=replace(config.fleet, enabled=False),
                             scheduler=replace(config.scheduler, status_file=base/'scheduler.json'),
                             brain=replace(config.brain, dry_run=False, economic_live=True, auto_solar=False,
                                           strategy=strategy, economy_speed=8, temperatures={},
                                           reserve={'metal':0,'crystal':0,'deuterium':0},
                                           build_goals=(Goal('metal_mine',6),Goal('crystal_mine',4)),
                                           research_goals=(Goal('energy_technology',3),),
                                           plan_file=base/'plan.json', journal_file=base/'orders.sqlite3'))
            clock = [0.0]
            state = snapshot()
            active: dict[str, tuple[float, Upgrade]] = {}
            clicks: list[Upgrade] = []
            reads: list[float] = []
            session = MagicMock(state=SessionState.READY, defense_paused=False, before_navigation=None)
            session.observe = AsyncMock()
            session.check = AsyncMock()
            journal = OrderJournal(config.brain.journal_file,'test-account')
            self.addCleanup(journal.close)

            def finish() -> None:
                for scope, (end, action) in list(active.items()):
                    if clock[0] >= end:
                        levels = state.research.data if action.kind == 'research' else state.bodies[0].buildings.data
                        levels[action.item] = action.target_level
                        del active[scope]

            def queue_html(scope: str) -> str:
                if scope in active:
                    end = datetime.now(timezone.utc)+timedelta(seconds=active[scope][0]-clock[0])
                    return f'<span class="countdown" data-end="{int(end.timestamp())}">Cargando</span>'
                return 'No hay ninguna investigación en progreso en este momento.' if scope=='research' else 'No hay edificios en construcción'

            @asynccontextmanager
            async def read_page(component: str, body_id: int):
                finish()
                page = MagicMock()
                catalog = RESEARCH if component=='research' else BUILD
                kind = 'research' if component=='research' else 'building'
                scope = 'research' if kind=='research' else 'building'

                def html() -> str:
                    finish()
                    levels = state.research.data if kind=='research' else state.bodies[0].buildings.data
                    cards = ''.join(f'<li class="technology" data-technology="{catalog[key].id}" aria-label="{catalog[key].name_es}"><span class="level">{value}</span></li>' for key,value in levels.items() if key in catalog)
                    resources = ''.join(f'<span id="resources_{key}" data-raw="{value}">{value}</span>' for key,value in state.bodies[0].resources.data.items())
                    return f'<meta name="ogame-planet-id" content="101"><span id="playerName">Test Commander</span><div class="smallplanet"><a class="planetlink" href="/game/index.php?cp=101"><span class="planet-name">Prueba</span><span class="planet-koords">[1:200:8]</span></a></div>{resources}<div id="attack_alert" data-status="noAttack"></div><div id="productionboxBottom"><div id="productionboxbuildingcomponent">{queue_html("building")}</div><div id="productionboxresearchcomponent">{queue_html("research")}</div></div>{cards}'

                page.content = AsyncMock(side_effect=html)

                def locator(selector: str):
                    card = MagicMock()
                    card.first.wait_for = AsyncMock()
                    card.count = AsyncMock(return_value=1)
                    if selector.startswith('.technology[data-technology="'):
                        identifier = int(selector.split('"')[1])
                        item = next(key for key,value in catalog.items() if value.id==identifier)
                        card.get_attribute = AsyncMock(return_value=catalog[item].name_es)
                        button = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), is_enabled=AsyncMock(return_value=True))

                        async def click(**kwargs):
                            self.assertNotIn(scope,active, 'Second click while its queue is occupied')
                            levels = state.research.data if kind=='research' else state.bodies[0].buildings.data
                            level = levels[item]
                            action = Upgrade(kind,item,101,'1:200:8',level,level+1,catalog[item].cost(level+1),'test')
                            self.assertIsNotNone(journal.pending_for(action), 'Click without durable receipt')
                            for key,cost in action.cost.items():
                                state.bodies[0].resources.data[key] -= cost
                                self.assertGreaterEqual(state.bodies[0].resources.data[key],0)
                            active[scope] = (clock[0]+(180 if kind=='research' else 60),action)
                            clicks.append(action)
                        button.click = AsyncMock(side_effect=click)
                        card.get_by_role.return_value = button
                    return card
                page.locator.side_effect = locator
                yield page

            session.read_page = read_page
            runtime = BotRuntime(config,session,journal,None)
            runtime.scheduler.clock = lambda:clock[0]

            async def scan(*args):
                reads.append(clock[0])
                finish()
                current = copy.deepcopy(state)
                current.finished_at = utc_now()
                for body in current.bodies:
                    body.resources = observed(body.resources.data)
                    body.buildings = observed(body.buildings.data)
                    body.construction_queue = observed({'state':'busy' if 'building' in active else 'free'})
                current.research = observed(current.research.data)
                current.research_queue = observed({'state':'busy' if 'research' in active else 'free'})
                current.movements = observed(current.movements.data)
                return current
            runtime.scanner.scan = AsyncMock(side_effect=scan)
            runtime.scanner.refresh_economy = AsyncMock(side_effect=scan)
            for job in runtime.scheduler.jobs.values():
                job.next_due=0
            await runtime.scheduler.run_due()
            self.assertEqual(len(clicks),2, 'Both independent queues should start immediately')
            self.assertEqual(len(journal.pending_upgrades()),2)
            self.assertEqual(runtime.latest.bodies[0].resources.data,state.bodies[0].resources.data)
            reads_before=len(reads)
            for moment in (3,10,30,59):
                clock[0]=moment
                await runtime.scheduler.run_due()
            self.assertEqual(len(reads),reads_before, 'Idle time caused extra screen navigation')
            # Completion-triggered decisions must chain all remaining goals.
            for _ in range(20):
                if len(clicks)==8 and not journal.pending_upgrades():
                    break
                relevant=[job.next_due for key,job in runtime.scheduler.jobs.items() if key in {'reconcile','brain'} and job.allowed() is None]
                clock[0]=max(clock[0]+1,min(relevant)+0.05)
                await runtime.scheduler.run_due()
            self.assertEqual(len(clicks),8)
            self.assertEqual(state.bodies[0].buildings.data['metal_mine'],6)
            self.assertEqual(state.bodies[0].buildings.data['crystal_mine'],4)
            self.assertEqual(state.research.data['energy_technology'],3)
            self.assertFalse(journal.pending_upgrades())
            self.assertEqual(runtime.latest.bodies[0].resources.data,state.bodies[0].resources.data)
            if strategy=='priority':
                self.assertEqual([a.item for a in clicks if a.kind=='building'],['metal_mine']*3+['crystal_mine']*2)
            journal.close()

    async def test_priority_chains_eight_orders_with_independent_queues(self):
        await self.run_chain('priority')

    async def test_roi_chains_eight_orders_with_independent_queues(self):
        await self.run_chain('roi')


class JournalMigrationTests(unittest.TestCase):
    def test_temperature_requires_explicit_unique_planet_range(self):
        from ogame_bot.scanner.dom import Document
        from ogame_bot.scanner.parsing import planet_temperature
        self.assertEqual(planet_temperature(Document('<div id="planetdata">Temperatura de -50°C a -10°C</div>')).data,{'minimum':-50,'maximum':-10})
        self.assertIsNone(planet_temperature(Document('<div id="planetdata">Temperatura desconocida</div>')).data)
        self.assertIsNone(planet_temperature(Document('<p>Temperatura de 10°C a 50°C</p>')).data)

    def test_roi_uses_scanned_temperature_when_manual_value_is_missing(self):
        from ogame_bot.brain.planner import Planner
        config=load_config(ROOT/'config.example.yaml')
        state=snapshot()
        state.bodies[0].temperature=observed({'minimum':0,'maximum':40})
        settings=replace(config.brain,research_enabled=False,strategy='roi',economy_speed=8,temperatures={},build_goals=(Goal('deuterium_synthesizer',4),))
        self.assertEqual(Planner(settings,120).plan(state)[0].item,'deuterium_synthesizer')

    def test_legacy_pending_receipt_migrates_without_loss_or_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'orders.sqlite3'
            action=Upgrade('research','energy_technology',101,'1:200:8',0,1,RESEARCH['energy_technology'].cost(1),'test')
            journal=OrderJournal(path,'test-account')
            identifier=journal.prepare(action)
            journal.close()
            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    connection.execute("UPDATE orders SET scope='upgrade'")
            journal=OrderJournal(path,'test-account')
            try:
                self.assertEqual(journal.pending_for(action)[0],identifier)
                building=Upgrade('building','metal_mine',101,'1:200:8',3,4,BUILD['metal_mine'].cost(4),'test')
                journal.prepare(building)
                self.assertEqual(len(journal.pending_upgrades()),2)
                from ogame_bot.errors import CoreError
                with self.assertRaises(CoreError):
                    journal.prepare(action)
            finally:
                journal.close()
