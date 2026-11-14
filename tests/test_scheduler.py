"""Plazos, prioridad, checkpoints, recuperación y cierre sin navegador real."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

from ogame_bot.config import load_config
from ogame_bot.core.session import SessionState
from ogame_bot.errors import ConfigurationError, CoreError, ReadError
from ogame_bot.scheduler.loop import DependencyPending, Scheduler, read_status
from ogame_bot.scheduler.runtime import BotRuntime
from ogame_bot.storage.orders import OrderJournal
from test_brain import snapshot, Planner

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = load_config(ROOT / "config.example.yaml").scheduler
        self.settings = replace(settings, status_file=Path(self.temp.name) / "scheduler.json")
        self.clock = Clock()
        self.scheduler = Scheduler(self.settings, clock=self.clock, uniform=lambda low, high: high)

    async def test_due_jobs_use_priority_not_registration_order(self):
        calls = []
        for name in ("fleet", "scanner", "defender", "brain"):
            async def callback(name=name): calls.append(name)
            self.scheduler.add(name, callback, lambda: (100, 100))
        self.assertEqual(await self.scheduler.run_due(), 4)
        self.assertEqual(calls, ["defender", "scanner", "brain", "fleet"])

    async def test_interval_starts_after_completion_without_catchup_burst(self):
        async def slow(): self.clock.advance(50)
        self.scheduler.add("scanner", slow, lambda: (10, 10))
        self.assertEqual(await self.scheduler.run_due(), 1)
        self.assertEqual(self.scheduler.jobs["scanner"].next_due, 160)
        self.assertEqual(await self.scheduler.run_due(), 0)

    async def test_checkpoint_runs_due_defense_between_scanner_pages(self):
        calls = []
        async def defend():
            calls.append("defense")
            await self.scheduler.checkpoint()
        async def scan():
            calls.append("page_1")
            self.clock.advance(101)
            await self.scheduler.checkpoint()
            calls.append("page_2")
        self.scheduler.add("defender", defend, lambda: (100, 100), immediate=False)
        self.scheduler.add("scanner", scan, lambda: (100, 100))
        await self.scheduler.run_due()
        self.assertEqual(calls, ["page_1", "defense", "page_2"])
        self.assertEqual(self.scheduler.jobs["defender"].runs, 1)

    async def test_checkpoint_does_not_run_lower_priority_or_overlap_job(self):
        calls = []
        async def scan():
            calls.append("scanner_start")
            self.assertFalse(await self.scheduler.run_now("scanner"))
            await self.scheduler.checkpoint()
            calls.append("scanner_end")
        async def fleet(): calls.append("fleet")
        self.scheduler.add("scanner", scan, lambda: (100, 100))
        self.scheduler.add("fleet", fleet, lambda: (100, 100))
        await self.scheduler.run_due()
        self.assertEqual(calls, ["scanner_start", "scanner_end", "fleet"])

    async def test_dependency_can_refresh_before_its_regular_deadline(self):
        calls = []
        async def scan(): calls.append("fresh_scan")
        async def brain():
            self.assertTrue(await self.scheduler.run_now("scanner"))
            calls.append("brain")
        self.scheduler.add("scanner", scan, lambda: (100, 100), immediate=False)
        self.scheduler.add("brain", brain, lambda: (100, 100))
        await self.scheduler.run_due()
        self.assertEqual(calls, ["fresh_scan", "brain"])

    async def test_paused_job_keeps_its_deadline_and_resumes_when_allowed(self):
        paused = True
        callback = AsyncMock()
        self.scheduler.add("brain", callback, lambda: (100, 100), allowed=lambda: "defender_paused" if paused else None)
        self.assertEqual(await self.scheduler.run_due(), 0)
        self.assertEqual(self.scheduler.status()["jobs"][0]["blocked_reason"], "defender_paused")
        paused = False
        self.assertEqual(await self.scheduler.run_due(), 1)
        callback.assert_awaited_once()

    async def test_retries_have_backoff_and_stop_at_limit(self):
        self.scheduler.add("brain", AsyncMock(side_effect=ReadError("Synthetic read failure")), lambda: (100, 100))
        await self.scheduler.run_due()
        job = self.scheduler.jobs["brain"]
        self.assertEqual(job.next_due, 105)
        self.clock.advance(5)
        await self.scheduler.run_due()
        self.assertEqual(job.next_due, 115)
        self.clock.advance(10)
        with self.assertRaises(CoreError):
            await self.scheduler.run_due()
        self.assertEqual(job.consecutive_failures, 3)

    async def test_deferral_does_not_consume_failure_budget(self):
        callback = AsyncMock(side_effect=[DependencyPending(), None])
        self.scheduler.add("brain", callback, lambda: (100, 100))
        await self.scheduler.run_due()
        job = self.scheduler.jobs["brain"]
        self.assertEqual((job.deferred, job.failures), (1, 0))
        self.clock.advance(self.settings.dependency_retry_seconds)
        await self.scheduler.run_due()
        self.assertEqual(job.runs, 1)

    async def test_critical_failure_stops_scheduler_without_logging_error_text(self):
        self.scheduler.add("defender", AsyncMock(side_effect=ValueError("PRIVATE_TEST_ERROR")), lambda: (100, 100), critical=True)
        self.scheduler.add("fleet", AsyncMock(), lambda: (100, 100))
        with self.assertRaises(ValueError):
            await self.scheduler.run()
        self.assertEqual(self.scheduler.state, "failed")
        self.assertEqual(self.scheduler.jobs["fleet"].runs, 0)
        serialized = self.settings.status_file.read_text()
        self.assertNotIn("PRIVATE_TEST_ERROR", serialized)
        self.assertEqual(json.loads(serialized)["last_error_type"], "ValueError")

    async def test_cancellation_records_stopped_and_releases_running_job(self):
        started = asyncio.Event()
        async def scan():
            started.set()
            await asyncio.Event().wait()
        self.scheduler.add("scanner", scan, lambda: (100, 100))
        task = asyncio.create_task(self.scheduler.run())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.scheduler.jobs["scanner"].running)
        self.assertEqual(self.scheduler.state, "stopped")
        self.assertEqual(self.scheduler.jobs["scanner"].last_result, "cancelled")

    async def test_graceful_stop_does_not_cancel_current_callback(self):
        calls = []
        async def scan():
            self.scheduler.request_stop()
            await asyncio.sleep(0)
            calls.append("completed")
        self.scheduler.add("scanner", scan, lambda: (100, 100))
        await self.scheduler.run()
        self.assertEqual(calls, ["completed"])
        self.assertEqual(self.scheduler.state, "stopped")

    async def test_stop_wakes_idle_scheduler_without_running_blocked_work(self):
        callback = AsyncMock()
        self.scheduler.add("brain", callback, lambda: (100, 100), allowed=lambda: "paused")
        task = asyncio.create_task(self.scheduler.run())
        await asyncio.sleep(0)
        self.scheduler.request_stop()
        await asyncio.wait_for(task, timeout=1)
        callback.assert_not_awaited()
        self.assertEqual(self.scheduler.state, "stopped")

    async def test_missing_status_is_explicit_and_old_heartbeat_is_detected(self):
        with self.assertRaises(CoreError): read_status(self.settings)
        self.scheduler.publish(force=True)
        self.assertTrue(read_status(self.settings)["heartbeat_is_recent"])
        data = json.loads(self.settings.status_file.read_text())
        data["heartbeat_valid_until"] = "2000-01-01T00:00:00+00:00"
        self.settings.status_file.write_text(json.dumps(data))
        self.assertFalse(read_status(self.settings)["heartbeat_is_recent"])

    async def test_confirmation_replay_is_idempotent(self):
        with OrderJournalForTest(Path(self.temp.name) / "orders.sqlite3") as journal:
            action = Planner(replace(load_config(ROOT / "config.example.yaml").brain, research_enabled=False), 120).plan(snapshot())[0]
            identifier = journal.prepare(action)
            journal.mark(identifier, "confirmed")
            journal.mark(identifier, "confirmed")
            self.assertIsNone(journal.pending())


class OrderJournalForTest(OrderJournal):
    def __init__(self, path): super().__init__(path, "scheduler-test")
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = load_config(ROOT / "config.example.yaml")
        self.config = replace(config, scheduler=replace(config.scheduler, status_file=Path(self.temp.name) / "scheduler.json"))
        self.session = MagicMock()
        self.session.state = SessionState.READY
        self.session.authentication_generation = 0
        self.session.before_navigation = None
        self.session.check = AsyncMock()
        self.session.observe = AsyncMock()
        self.journal = MagicMock()
        self.journal.pending.return_value = None
        self.journal.pending_upgrades.return_value = []
        self.defender = MagicMock()
        self.defender.last_state = "clear"
        async def defend(): self.session.defense_paused = False
        self.defender.process = AsyncMock(side_effect=defend)
        self.runtime = BotRuntime(self.config, self.session, self.journal, self.defender)
        self.runtime.scanner.scan = AsyncMock(return_value=snapshot())
        self.runtime.scanner.refresh_economy = AsyncMock(return_value=snapshot())
        self.runtime.brain.process = AsyncMock(return_value={"result": {"state": "no_action"}, "notes": []})
        self.runtime.fleet.process = AsyncMock()

    async def test_initial_defense_precedes_scan_and_automation(self):
        order = []
        async def defend():
            order.append("defense")
            self.session.defense_paused = False
        async def scan():
            order.append("scan")
            state = snapshot()
            from test_brain import observed
            state.bodies[0].ships = observed({})
            return state
        async def brain(*args):
            order.append("brain")
            return {"result": {"state": "no_action"}, "notes": []}
        async def fleet(*args):
            order.append("fleet")
            return {"result":{"state":"no_action"}}
        self.defender.process.side_effect = defend
        self.runtime.scanner.scan.side_effect = scan
        self.runtime.brain.process.side_effect = brain
        self.runtime.fleet.process.side_effect = fleet
        await self.runtime.scheduler.run_due()
        self.assertEqual(order, ["defense", "scan", "brain", "fleet"])

    async def test_normal_session_health_does_not_change_defense_interval(self):
        job = self.runtime.scheduler.jobs["defender"]
        job.next_due += 1000
        deadline = job.next_due
        self.session.defense_paused = False
        await self.runtime._session()
        self.assertEqual(job.next_due, deadline)
        self.assertFalse(self.session.defense_paused)

    async def test_reauthentication_requires_new_defense_before_automation(self):
        self.session.state = SessionState.WAITING_LOGIN
        async def recover(): self.session.state = SessionState.READY
        self.session.check.side_effect = recover
        await self.runtime._session()
        self.assertTrue(self.session.defense_paused)
        self.assertLessEqual(self.runtime.scheduler.jobs["defender"].next_due, self.runtime.scheduler.clock())

    async def test_expired_snapshot_is_refreshed_before_brain(self):
        self.session.defense_paused = False
        self.runtime.latest = snapshot()
        self.runtime._snapshot_finished = float("-inf")
        # Evita otra tarea inicial: aquí se comprueba la dependencia explícita.
        self.runtime.scheduler.jobs["defender"].next_due += 1000
        await self.runtime._brain()
        self.runtime.scanner.refresh_economy.assert_awaited_once()
        self.runtime.brain.process.assert_awaited_once()

    async def test_defense_mode_has_no_economic_jobs(self):
        runtime = BotRuntime(self.config, self.session, self.journal, self.defender, mode="defender")
        self.assertEqual(set(runtime.scheduler.jobs), {"session", "defender", "reconcile"})

    async def test_scanner_mode_has_no_dispatch_or_defense(self):
        runtime = BotRuntime(self.config, self.session, None, None, mode="scanner")
        self.assertEqual(set(runtime.scheduler.jobs), {"session", "scanner"})


class SettingsTests(unittest.TestCase):
    def test_invalid_priorities_and_colliding_state_path_are_rejected(self):
        for change in ("priority", "path", "retry"):
            data = yaml.safe_load((ROOT / "config.example.yaml").read_text())
            if change == "priority": data["scheduler"]["priorities"]["defender"] = 100
            elif change == "path": data["scheduler"]["status_file"] = data["scanner"]["output_file"]
            else: data["scheduler"]["retry_max_seconds"] = 1
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(yaml.safe_dump(data))
                with self.assertRaises(ConfigurationError): load_config(path)


if __name__ == "__main__":
    unittest.main()
