"""Satellite energy, native contracts, one-shot saves and virtual-day recovery."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ogame_bot.brain.planner import Planner, Upgrade
from ogame_bot.config import load_config
from ogame_bot.core.action_executor import uses_real_resources
from ogame_bot.domain.models import Reading
from ogame_bot.errors import CoreError, ReadError, LoginRequired
from ogame_bot.fleet.executor import real_flight_enabled
from ogame_bot.fleet.fleetsave import plan_fleetsave
from ogame_bot.fleet.models import SHIP_IDS
from ogame_bot.fleet.native import native_outgoing, native_debris
from ogame_bot.scanner.dom import Document
from ogame_bot.scheduler.loop import Scheduler
from ogame_bot.storage.orders import OrderJournal
from test_brain import observed, snapshot
from test_fleet import account, settings
import test_fleet as fleet_tests
import test_defender as defense_tests

ROOT=Path(__file__).resolve().parents[1]


class SatelliteEnergyTests(unittest.TestCase):
    def setUp(self):
        config=load_config(ROOT/'config.example.yaml')
        self.settings=replace(config.brain,research_enabled=False,energy_source='solar_satellite',auto_solar=True,economic_live=True,temperatures={101:40},max_satellites=20)
        self.state=snapshot()
        self.state.bodies[0].ships=observed({'solar_satellite':3})
        self.state.bodies[0].shipyard_queue=observed({'state':'free'})

    def test_energy_deficit_creates_bounded_satellite_batch_and_uses_economy_permission(self):
        self.state.bodies[0].resources.data['energy']=-100
        action=Planner(self.settings,120).plan(self.state)[0]
        self.assertEqual((action.kind,action.item,action.quantity),('shipyard','solar_satellite',4))
        self.assertEqual(action.cost,{'metal':0,'crystal':8000,'deuterium':2000})
        self.assertEqual(Upgrade.from_dict(action.to_dict()),action)
        self.assertTrue(uses_real_resources(action,self.settings))
        self.assertFalse(uses_real_resources(action,replace(self.settings,economic_live=False,dry_run=True)))

    def test_unknown_temperature_does_not_guess_a_yield_but_override_works(self):
        self.state.bodies[0].resources.data['energy']=-100
        settings=replace(self.settings,temperatures={})
        self.assertEqual(Planner(settings,120).plan(self.state),[])
        action=Planner(replace(settings,satellite_energy_per_unit=50),120).plan(self.state)[0]
        self.assertEqual(action.quantity,2)

    def test_satellites_are_not_rebuilt_when_energy_is_already_sufficient(self):
        action=Planner(self.settings,120).plan(self.state)[0]
        self.assertEqual(action.kind,'building')

    def test_satellite_limit_and_busy_shipyard_are_respected(self):
        self.state.bodies[0].resources.data['energy']=-100
        self.assertEqual(Planner(replace(self.settings,max_satellites=3),120).plan(self.state),[])
        self.state.bodies[0].shipyard_queue=observed({'state':'busy'})
        self.assertEqual(Planner(self.settings,120).plan(self.state),[])

    def test_roi_can_choose_satellites_for_next_mine_without_solar_levels(self):
        self.state.bodies[0].resources.data['energy']=0
        actions=Planner(replace(self.settings,strategy='roi',economy_speed=8,max_payback_days=10000),120).plan(self.state)
        self.assertEqual(actions[0].item,'solar_satellite')
        self.assertGreater(actions[0].payback_days,0)


class PlanetCapacityTests(unittest.TestCase):
    def test_verified_storage_capacity_and_fields_are_parsed_without_guessing(self):
        from ogame_bot.scanner.parsing import storage_capacity,planet_fields
        doc=Document('<div id="planetdata"><li class="fields">Campos (20/200)</li></div><span id="resources_metal" data-max="10000"></span><span id="resources_crystal" data-capacity="20000"></span><span id="resources_deuterium" data-storage="10000"></span>')
        self.assertEqual(planet_fields(doc).data,{'used':20,'total':200,'free':180})
        self.assertEqual(storage_capacity(doc).data,{'metal':10000,'crystal':20000,'deuterium':10000})
        self.assertIsNone(storage_capacity(Document('<p>Cargando</p>')).data)
        tooltip=Document('<span id="resources_metal"></span><li id="metal_box"><span title="&lt;table&gt;&lt;tr&gt;&lt;td&gt;Capacidad de almacenamiento:&lt;/td&gt;&lt;td&gt;40.000&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"></span></li>')
        self.assertEqual(storage_capacity(tooltip).data['metal'],40000)

    def test_fullest_storage_preempts_roi_and_priority_then_returns_to_mines(self):
        from ogame_bot.brain.settings import Goal
        config=load_config(ROOT/'config.example.yaml')
        for mode in ('roi','priority'):
            state=snapshot()
            state.bodies[0].buildings.data.update(metal_storage=1,crystal_storage=1,deuterium_tank=1)
            state.bodies[0].resources.data.update(metal=9500,crystal=10000,deuterium=1000)
            state.bodies[0].storage_capacity=observed({'metal':10000,'crystal':10000,'deuterium':10000})
            state.bodies[0].fields=observed({'used':20,'total':200,'free':180})
            settings=replace(config.brain,strategy=mode,economy_speed=8,research_enabled=False,reserve={'metal':0,'crystal':0,'deuterium':0},build_goals=(Goal('metal_mine',5),))
            planner=Planner(settings,120)
            self.assertEqual(planner.plan(state)[0].item,'crystal_storage')
            state.bodies[0].resources.data.update(metal=3000,crystal=3000)
            self.assertEqual(planner.plan(state)[0].item,'metal_mine')

    def test_full_fields_block_buildings_but_allow_satellites_for_existing_deficit(self):
        state=snapshot()
        state.bodies[0].fields=observed({'used':200,'total':200,'free':0})
        config=load_config(ROOT/'config.example.yaml')
        settings=replace(config.brain,research_enabled=False)
        self.assertEqual(Planner(settings,120).plan(state),[])
        state.bodies[0].resources.data['energy']=-100
        state.bodies[0].ships=observed({'solar_satellite':0})
        state.bodies[0].shipyard_queue=observed({'state':'free'})
        action=Planner(replace(settings,energy_source='solar_satellite',satellite_energy_per_unit=30),120).plan(state)[0]
        self.assertEqual(action.item,'solar_satellite')


def save_state():
    state=account()
    inventory=state.bodies[0].ships.data
    state.bodies[0].ships=observed({name:inventory.get(name,0) for name in SHIP_IDS})
    return state


def save_settings():
    fleet=settings()
    return replace(fleet,capacities={**fleet.capacities,'espionage_probe':0},fleetsave={**fleet.fleetsave,'enabled':True,'origin_id':101,'destination_id':102,'plan_id':'night-save','minimum_flight_seconds':120})


class ScheduledSaveTests(unittest.TestCase):
    def test_own_deployment_requires_real_inventory_and_uses_reserved_slot(self):
        state=save_state()
        state.fleet_slots.data.update(fleet_used=15,fleet_total=16)
        flight,reason=plan_fleetsave(state,save_settings(),{'metal':1,'crystal':1.5,'deuterium':2.5},120)
        self.assertEqual(reason,'fleetsave_planned')
        self.assertEqual((flight.mission,flight.destination_id,flight.purpose),(4,102,'fleetsave'))
        self.assertEqual(flight.ships,{key:value for key,value in state.bodies[0].ships.data.items() if value>0})
        flight.validate()

    def test_future_schedule_and_unknown_destinations_do_not_dispatch(self):
        cfg=save_settings()
        future=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.assertEqual(plan_fleetsave(save_state(),replace(cfg,fleetsave={**cfg.fleetsave,'run_at':future}),{'metal':1,'crystal':1,'deuterium':1},120)[1],'fleetsave_waiting_schedule')
        state=save_state()
        state.registry=Reading.unavailable('unknown')
        self.assertIsNone(plan_fleetsave(state,cfg,{'metal':1,'crystal':1,'deuterium':1},120)[0])

    def test_confirmed_plan_survives_restart_and_is_not_repeated(self):
        flight,_=plan_fleetsave(save_state(),save_settings(),{'metal':1,'crystal':1,'deuterium':1},120)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'orders.sqlite3'
            journal=OrderJournal(path,'test')
            identifier=journal.prepare(flight)
            self.assertFalse(journal.fleetsave_completed('night-save'))
            journal.mark(identifier,'confirmed')
            journal.close()
            journal=OrderJournal(path,'test')
            try:
                self.assertTrue(journal.fleetsave_completed('night-save'))
                self.assertFalse(journal.fleetsave_completed('another-save'))
            finally:
                journal.close()

    def test_live_save_gate_is_independent_of_other_missions(self):
        flight,_=plan_fleetsave(save_state(),save_settings(),{'metal':1,'crystal':1,'deuterium':1},120)
        config=load_config(ROOT/'config.example.yaml')
        config=replace(config,brain=replace(config.brain,dry_run=True),fleet=replace(save_settings(),fleetsave={**save_settings().fleetsave,'live':True}))
        self.assertTrue(real_flight_enabled(flight,config))

    def test_defender_can_plan_when_fuel_ceiling_exceeds_available_deuterium(self):
        state=defense_tests.threatened()
        state.bodies[0].resources.data['deuterium']=500
        config=defense_tests.config()
        from ogame_bot.defender.threats import analyze
        from ogame_bot.defender.planner import EscapePlanner
        _,threats=analyze(state.movements,state.registry,config.defender)
        action=EscapePlanner(config.defender,{**config.fleet.capacities,'espionage_probe':0},config.brain.resource_weights).plan(state,threats[0])
        self.assertIsNotNone(action)


class NativeMovementTests(unittest.TestCase):
    def document(self, extra: str=''):
        return Document('<div id="movement"><div id="fleet77" class="fleetDetails" data-mission-type="3"><span class="originCoords">[1:200:8]</span><span class="destinationCoords">[1:200:9]</span><div class="destinationPlanet"><figure class="planet"></figure></div><div class="route"><a href="#payload" class="fleet_icon_forward">Detalles</a></div></div><div id="payload"><table><tr><td>Nave pequeña de carga</td><td>2</td></tr><tr><td>Metal:</td><td>1.000</td></tr><tr><td>Cristal:</td><td>200</td></tr><tr><td>Deuterio:</td><td>0</td></tr>'+extra+'</table></div></div>')

    def test_visible_movement_payload_is_exact_and_ambiguous_ship_is_rejected(self):
        reading=native_outgoing(self.document())
        self.assertEqual(reading.status,'complete')
        self.assertEqual(reading.data[0]['ships'],{'small_cargo':2})
        self.assertEqual(reading.data[0]['resources'],{'metal':1000,'crystal':200,'deuterium':0})
        self.assertNotEqual(native_outgoing(self.document('<tr><td>Nave desconocida</td><td>1</td></tr>')).status,'complete')

    def test_empty_list_requires_explicit_message(self):
        self.assertEqual(native_outgoing(Document('<div id="movement">No hay flotas en movimiento.</div>')).data,[])
        self.assertIsNone(native_outgoing(Document('<div id="movement">Cargando</div>')).data)

    def test_debris_requires_verified_location_and_explicit_resource_amounts(self):
        doc=Document('<div id="galaxyContent"><div class="galaxyRow" data-position="8"><div class="debris"><table><tr><td>Metal</td><td>10.000</td></tr><tr><td>Cristal</td><td>2.000</td></tr></table></div></div></div>')
        self.assertIsNone(native_debris(doc,1,200).data)
        self.assertEqual(native_debris(doc,1,200,location_verified=True).data,[{'coordinates':'1:200:8','metal':10000,'crystal':2000}])


class FleetModuleExecutionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fleet_tests.FleetTransactionTests.setUp

    async def test_transport_live_gate_executes_and_confirms_with_global_simulation(self):
        from ogame_bot.fleet.planner import FleetPlanner
        from ogame_bot.fleet.executor import FleetExecutor
        cfg=replace(self.config.fleet,expeditions={**self.config.fleet.expeditions,'enabled':False},transports={**self.config.fleet.transports,'enabled':True,'live':True,'sources':[101],'destination_id':102})
        self.config=replace(self.config,brain=replace(self.config.brain,dry_run=True),fleet=cfg)
        self.flight=FleetPlanner(cfg,120).plan(account())[0]
        self.assertEqual(self.flight.mission,3)
        self.page.locator(cfg.selectors['capacity']).inner_text.return_value=str(self.flight.ships['large_cargo']*25000)
        def html():
            pending=self.journal.pending('fleet')
            flight=pending[1] if self.sent and pending else self.flight
            return fleet_tests.rendered(flight,sent=self.sent)
        self.page.content=AsyncMock(side_effect=html)
        result=await FleetExecutor(self.session,self.config,self.journal).execute(self.flight)
        self.assertEqual(result['state'],'confirmed')
        self.assertLessEqual(int(self.controls[cfg.selectors['resource_input'].format(resource='deuterium')].value),1000000-100-cfg.transports['leave_resources']['deuterium'])

    async def test_recycling_rechecks_debris_before_confirmed_send(self):
        from ogame_bot.fleet.planner import FleetPlanner
        from ogame_bot.fleet.executor import FleetExecutor
        cfg=replace(self.config.fleet,expeditions={**self.config.fleet.expeditions,'enabled':False},recycling={**self.config.fleet.recycling,'enabled':True,'live':True,'origins':[101],'radius_systems':1})
        self.config=replace(self.config,brain=replace(self.config.brain,dry_run=True),fleet=cfg)
        self.flight=FleetPlanner(cfg,120).plan(account())[0]
        self.assertEqual(self.flight.mission,8)
        with patch('ogame_bot.fleet.executor.FleetReader') as reader:
            reader.return_value.galaxy=AsyncMock(return_value=account().debris)
            result=await FleetExecutor(self.session,self.config,self.journal).execute(self.flight)
            reader.return_value.galaxy.assert_awaited_once()
        self.assertEqual(result['state'],'confirmed')

    async def test_scheduled_save_reloads_actual_resources_and_cannot_repeat(self):
        from ogame_bot.fleet.executor import FleetExecutor
        cfg=save_settings()
        cfg=replace(cfg,fleetsave={**cfg.fleetsave,'live':True})
        self.config=replace(self.config,brain=replace(self.config.brain,dry_run=True),fleet=cfg)
        self.flight,_=plan_fleetsave(save_state(),cfg,self.config.brain.resource_weights,120)
        self.session.defense_paused=False
        self.page.locator(self.config.fleet.selectors['flight_seconds']).get_attribute.return_value='180'
        def html():
            pending=self.journal.pending('fleet')
            flight=pending[1] if self.sent and pending else self.flight
            return defense_tests.defense_html(flight,sent=self.sent,clear=True)
        self.page.content=AsyncMock(side_effect=html)
        executor=FleetExecutor(self.session,self.config,self.journal)
        result=await executor.execute(self.flight)
        self.assertEqual(result['state'],'confirmed')
        self.assertTrue(self.journal.fleetsave_completed('night-save'))
        self.assertEqual((await executor.execute(self.flight))['state'],'blocked')
        self.controls[self.config.fleet.selectors['send']].click.assert_awaited_once()


class VirtualDayStabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_virtual_day_keeps_defense_running_during_recoverable_module_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=replace(load_config(ROOT/'config.example.yaml').scheduler,status_file=Path(directory)/'status.json',isolate_failures=True,max_consecutive_failures=2,retry_initial_seconds=1,retry_max_seconds=60)
            clock=[0.0]
            counts={'defender':0,'brain':0,'session':0}
            healthy=[True]
            def recover(name,error):
                if isinstance(error,LoginRequired):
                    healthy[0]=False
            scheduler=Scheduler(cfg,clock=lambda:clock[0],uniform=lambda low,high:high,on_recoverable=recover)
            async def defend():counts['defender']+=1
            async def session():
                counts['session']+=1
                if counts['session'] in {10,11,45}:
                    raise LoginRequired('synthetic expired session')
                healthy[0]=True
            async def brain():
                counts['brain']+=1
                if counts['brain']<=12:
                    raise ReadError('synthetic transient DOM change')
            scheduler.add('defender',defend,lambda:(300,300),critical=True)
            scheduler.add('session',session,lambda:(450,450))
            scheduler.add('brain',brain,lambda:(600,600),allowed=lambda:None if healthy[0] else 'session_not_ready')
            for minute in range(1441):
                clock[0]=minute*60
                await scheduler.run_due()
            self.assertGreaterEqual(counts['defender'],288)
            self.assertGreater(counts['brain'],100)
            self.assertGreater(scheduler.jobs['brain'].runs,80)
            self.assertTrue(healthy[0])
            self.assertEqual(scheduler.jobs['brain'].failures,12)

    async def test_critical_failure_still_stops_instead_of_claiming_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=replace(load_config(ROOT/'config.example.yaml').scheduler,status_file=Path(directory)/'state.json',isolate_failures=True)
            scheduler=Scheduler(cfg)
            scheduler.add('defender',AsyncMock(side_effect=ReadError('unknown defense')),lambda:(10,10),critical=True)
            with self.assertRaises(CoreError):
                await scheduler.run_due()
