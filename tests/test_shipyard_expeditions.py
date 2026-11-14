"""Finite ship production, expedition composition and independent live gates."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

from ogame_bot.brain.catalog import SHIP_BUILD
from ogame_bot.brain.planner import Upgrade
from ogame_bot.brain.service import Brain
from ogame_bot.brain.settings import ShipGoal
from ogame_bot.brain.shipyard import plan_shipyard
from ogame_bot.config import load_config
from ogame_bot.core.action_executor import uses_real_resources
from ogame_bot.core.action_executor import ActionExecutor
from ogame_bot.fleet.executor import FleetExecutor
from ogame_bot.domain.models import Reading
from ogame_bot.errors import ConfigurationError, CoreError
from ogame_bot.fleet.planner import FleetPlanner
from ogame_bot.fleet.service import FleetManager
from ogame_bot.storage.orders import OrderJournal
from test_brain import observed, snapshot
from test_fleet import account, settings
import test_probe_bootstrap as bootstrap_tests
import test_fleet as fleet_tests

ROOT=Path(__file__).resolve().parents[1]


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'orders.sqlite3'
        self.journal=OrderJournal(self.path,'test-account')
        self.addCleanup(self.journal.close)
        self.config=load_config(ROOT/'config.example.yaml')
        self.settings=replace(self.config.brain,shipyard_enabled=True,shipyard_live=True,shipyard_max_batch=3,reserve={'metal':0,'crystal':0,'deuterium':100},shipyard_queue=(ShipGoal('cargo-order',101,'small_cargo',8),))
        self.state=snapshot()
        self.state.bodies[0].ships=observed({'small_cargo':0})
        self.state.bodies[0].shipyard_queue=observed({'state':'free'})

    def plan(self):
        return plan_shipyard(self.state,self.settings,self.journal,120)[0]

    def test_batches_chain_then_do_not_repeat_when_stock_leaves_or_process_restarts(self):
        quantities=[]
        for expected in (3,3,2):
            action=self.plan()[0]
            self.assertEqual(action.quantity,expected)
            self.assertEqual(Upgrade.from_dict(action.to_dict()),action)
            identifier=self.journal.prepare(action)
            for resource,amount in action.cost.items():
                self.state.bodies[0].resources.data[resource]-=amount
            self.state.bodies[0].ships.data['small_cargo']+=expected
            self.journal.mark(identifier,'confirmed')
            quantities.append(action.quantity)
        self.assertEqual(quantities,[3,3,2])
        self.assertEqual(self.plan(),[])
        self.state.bodies[0].ships.data['small_cargo']=0
        self.journal.close()
        self.journal=OrderJournal(self.path,'test-account')
        self.addCleanup(self.journal.close)
        self.assertEqual(self.plan(),[])
        self.assertEqual(self.journal.produced('cargo-order',101,'small_cargo'),8)

    def test_batch_is_limited_by_resources_and_reserves(self):
        self.state.bodies[0].resources.data.update(metal=5000,crystal=3000)
        action=self.plan()[0]
        self.assertEqual(action.quantity,1)
        self.assertEqual(action.cost,{'metal':2000,'crystal':2000,'deuterium':0})

    def test_unknown_inventory_busy_queue_and_threat_each_block_production(self):
        self.state.bodies[0].shipyard_queue=observed({'state':'busy'})
        self.assertEqual(self.plan(),[])
        self.state.bodies[0].shipyard_queue=observed({'state':'free'})
        self.state.bodies[0].ships=Reading.unavailable('unknown')
        self.assertEqual(self.plan(),[])
        self.state.bodies[0].ships=observed({'small_cargo':0})
        self.state.movements=observed({'threat_status':'detected'})
        self.assertEqual(self.plan(),[])

    def test_goal_identity_cannot_be_reused_for_another_ship(self):
        action=self.plan()[0]
        self.journal.prepare(action)
        with self.assertRaises(CoreError):
            self.journal.produced('cargo-order',101,'large_cargo')

    def test_hangar_live_does_not_enable_other_economic_actions(self):
        action=self.plan()[0]
        self.assertTrue(uses_real_resources(action,replace(self.settings,dry_run=True,economic_live=False)))
        self.assertFalse(uses_real_resources(action,replace(self.settings,shipyard_live=False,dry_run=False)))

    def test_pending_uncertain_batch_blocks_duplicate_and_progress_is_not_counted(self):
        action=self.plan()[0]
        identifier=self.journal.prepare(action)
        self.journal.mark(identifier,'uncertain')
        self.assertEqual(self.journal.produced('cargo-order',101,'small_cargo'),0)
        with self.assertRaises(CoreError):
            self.journal.prepare(action)


class ExpeditionConfigurationTests(unittest.TestCase):
    def test_panel_saves_production_and_expeditions_without_disabling_global_simulation(self):
        from ogame_bot.panel_service import PanelService
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.yaml'
            path.write_bytes((ROOT/'config.example.yaml').read_bytes())
            service=PanelService(path)
            payload=service.settings()
            payload['shipyard']={'enabled':True,'live':True,'max_batch':3,'queue':[{'id':'panel-cargo','body_id':101,'item':'small_cargo','quantity':8}]}
            payload['fleet']['expeditions'].update(enabled=True,live=True,composition_mode='manual',manual_ships={'small_cargo':1},origins=[101])
            service.save_settings(payload)
            saved=load_config(path)
            self.assertTrue(saved.brain.dry_run)
            self.assertTrue(saved.brain.shipyard_live)
            self.assertTrue(saved.fleet.expeditions['live'])
            self.assertEqual(PanelService(path).settings()['shipyard']['queue'][0]['id'],'panel-cargo')

    def test_manual_small_fleet_does_not_require_top1_or_effective_cargo_capacity(self):
        cfg=settings()
        cfg=replace(cfg,capacities={},expeditions={**cfg.expeditions,'composition_mode':'manual','manual_ships':{'large_cargo':2,'espionage_probe':1},'max_parallel':2})
        state=account()
        state.top1=Reading.unavailable('not_needed')
        action=FleetPlanner(cfg,120).plan(state)[0]
        self.assertEqual(action.ships,{'large_cargo':2,'espionage_probe':1})
        self.assertEqual(action.destination,'1:200:16')
        self.assertEqual(action.resources,{'metal':0,'crystal':0,'deuterium':0})
        state.fleet_slots.data['expedition_used']=2
        self.assertEqual(FleetPlanner(cfg,120).plan(state),[])

    def test_new_configuration_is_optional_and_rejects_invalid_quantities(self):
        raw=yaml.safe_load((ROOT/'config.example.yaml').read_bytes())
        raw['shipyard']={'enabled':True,'live':False,'max_batch':10,'queue':[{'id':'one','body_id':101,'item':'small_cargo','quantity':2}]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.yaml'
            path.write_text(yaml.safe_dump(raw),encoding='utf-8')
            config=load_config(path)
            self.assertEqual(config.brain.shipyard_queue[0].quantity,2)
            raw['shipyard']['queue'][0]['quantity']=True
            path.write_text(yaml.safe_dump(raw),encoding='utf-8')
            with self.assertRaises(ConfigurationError):
                load_config(path)


class ExpeditionLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_fleet_form_is_reported_without_taking_down_other_modules(self):
        from ogame_bot.errors import ReadError
        with tempfile.TemporaryDirectory() as directory:
            config=load_config(ROOT/'config.example.yaml')
            config=replace(config,fleet=replace(settings(),plan_file=Path(directory)/'fleet.json'),scanner_output_file=Path(directory)/'state.json')
            journal=MagicMock()
            journal.pending.return_value=None
            journal.pending_upgrades.return_value=[]
            executor=MagicMock(execute=AsyncMock(side_effect=ReadError('Selector de flota ausente o ambiguo: send')))
            manager=FleetManager(config,MagicMock(enrich=AsyncMock()),executor,journal)
            report=await manager.process(account())
            self.assertEqual(report['result']['reason'],'fleet_form_unverified')
            self.assertEqual(json.loads(config.fleet.plan_file.read_text())['result']['state'],'blocked')

    async def test_available_slots_fill_then_one_return_allows_exactly_one_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load_config(ROOT/'config.example.yaml')
            fleet=replace(settings(),plan_file=Path(directory)/'fleet.json',expeditions={**settings().expeditions,'composition_mode':'manual','manual_ships':{'large_cargo':2},'max_parallel':2,'live':False})
            config=replace(config,fleet=fleet,scanner_output_file=Path(directory)/'state.json')
            state=account()
            count=[0]
            executor=MagicMock()
            async def execute(action):
                state.fleet_slots.data['expedition_used']+=1
                state.fleet_slots.data['fleet_used']+=1
                state.bodies[0].ships.data['large_cargo']-=2
                count[0]+=1
                return {'state':'confirmed','fleet_id':str(count[0])}
            executor.execute=AsyncMock(side_effect=execute)
            reader=MagicMock(enrich=AsyncMock())
            journal=MagicMock()
            journal.pending.return_value=None
            journal.pending_upgrades.return_value=[]
            manager=FleetManager(config,reader,executor,journal)
            await manager.process(state)
            await manager.process(state)
            self.assertEqual((await manager.process(state))['result']['state'],'no_action')
            self.assertEqual(count[0],2)
            state.fleet_slots.data['expedition_used']-=1
            state.fleet_slots.data['fleet_used']-=1
            state.bodies[0].ships.data['large_cargo']+=2
            await manager.process(state)
            self.assertEqual(count[0],3)

    async def test_unconfirmed_fabrication_prevents_fleet_using_confirmation_stock(self):
        config=load_config(ROOT/'config.example.yaml')
        with tempfile.TemporaryDirectory() as directory:
            fleet=replace(settings(),plan_file=Path(directory)/'fleet.json')
            config=replace(config,fleet=fleet,scanner_output_file=Path(directory)/'state.json')
            journal=MagicMock()
            journal.pending.return_value=None
            action=Upgrade('shipyard','espionage_probe',101,'1:200:8',0,1,SHIP_BUILD['espionage_probe'].cost(1),'test')
            journal.pending_upgrades.return_value=[('order',action)]
            executor=MagicMock(execute=AsyncMock())
            manager=FleetManager(config,MagicMock(enrich=AsyncMock()),executor,journal)
            self.assertEqual((await manager.process(account()))['result']['state'],'no_action')
            executor.execute.assert_not_awaited()


class BatchExecutorTests(unittest.IsolatedAsyncioTestCase):
    setUp = bootstrap_tests.BootstrapExecutionTests.setUp
    html = bootstrap_tests.BootstrapExecutionTests.html

    async def test_incoming_stock_does_not_confirm_an_unfinished_shipyard_queue(self):
        config=replace(self.config,brain=replace(self.config.brain,probe_target=0,shipyard_enabled=True,shipyard_live=True,shipyard_queue=(ShipGoal('incoming-stock',101,'espionage_probe',3),)))
        action=Upgrade('shipyard','espionage_probe',101,'1:200:8',0,3,{'metal':0,'crystal':3000,'deuterium':0},'production_queue',production_key='incoming-stock')
        self.journal.prepare(action)
        original=self.html
        self.html=lambda:original().replace('<span class="amount">0</span>','<span class="amount">3</span>').replace('<td class="idle">No se están construyendo naves o defensas.</td>','<span class="countdown">00:03:00</span>')
        result=await ActionExecutor(self.session,config,self.journal).reconcile()
        self.assertEqual(result['state'],'pending')
        self.assertEqual(self.journal.produced('incoming-stock',101,'espionage_probe'),0)

    async def test_real_executor_fills_batch_and_confirmed_goal_does_not_repeat_after_departure(self):
        config=replace(self.config,brain=replace(self.config.brain,probe_target=0,probe_bootstrap_live=False,builder_enabled=False,research_enabled=False,shipyard_enabled=True,shipyard_live=True,shipyard_max_batch=3,shipyard_queue=(ShipGoal('three-probes',101,'espionage_probe',3),)))
        original=self.html
        stock=[0]
        self.html=lambda:original().replace('<span class="amount">0</span>',f'<span class="amount">{stock[0]}</span>')
        async def click(**kwargs):
            self.assertEqual(self.quantity,'3')
            self.assertIsNotNone(self.journal.pending('upgrade'))
            stock[0]=3
        self.sender.click.side_effect=click
        state=snapshot()
        state.bodies[0].ships=observed({'espionage_probe':0})
        state.bodies[0].shipyard_queue=observed({'state':'free'})
        action=plan_shipyard(state,config.brain,self.journal,120)[0][0]
        executor=ActionExecutor(self.session,config,self.journal)
        self.assertEqual((await executor.execute(action))['state'],'pending')
        self.assertEqual((await executor.reconcile())['state'],'confirmed')
        stock[0]=0
        report=await Brain(config,executor,self.journal).process(state)
        self.assertEqual(report['result']['state'],'no_action')
        self.sender.click.assert_awaited_once()

    async def test_batch_cost_is_checked_instead_of_single_unit_cost(self):
        config=replace(self.config,brain=replace(self.config.brain,probe_target=0,probe_bootstrap_live=False,shipyard_enabled=True,shipyard_live=True,shipyard_max_batch=15,shipyard_queue=(ShipGoal('fifteen',101,'espionage_probe',15),)))
        action=Upgrade('shipyard','espionage_probe',101,'1:200:8',0,15,{'metal':0,'crystal':15000,'deuterium':0},'production_queue',production_key='fifteen')
        result=await ActionExecutor(self.session,config,self.journal).execute(action)
        self.assertEqual(result['reason'],'resources_or_reserve_insufficient')
        self.assertIsNone(self.journal.pending())
        self.sender.click.assert_not_awaited()


class ExpeditionLiveGateTests(unittest.IsolatedAsyncioTestCase):
    setUp = fleet_tests.FleetTransactionTests.setUp

    async def test_expedition_live_runs_with_global_simulation_kept_enabled(self):
        config=replace(self.config,brain=replace(self.config.brain,dry_run=True),fleet=replace(self.config.fleet,expeditions={**self.config.fleet.expeditions,'live':True}))
        result=await FleetExecutor(self.session,config,self.journal).execute(self.flight)
        self.assertEqual(result['state'],'confirmed')
        self.assertTrue(self.sent)

    async def test_explicit_expedition_simulation_does_not_fill_form(self):
        config=replace(self.config,brain=replace(self.config.brain,dry_run=True),fleet=replace(self.config.fleet,expeditions={**self.config.fleet.expeditions,'live':False}))
        result=await FleetExecutor(self.session,config,self.journal).execute(self.flight)
        self.assertEqual(result['state'],'simulated')
        self.assertFalse(self.controls)
