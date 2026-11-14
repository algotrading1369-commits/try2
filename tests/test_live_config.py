"""Instruction acknowledgement and reload boundaries; no live game actions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from ogame_bot.config import load_config
from ogame_bot.core.session import SessionState
from ogame_bot.live_config import config_version
from ogame_bot.panel_service import PanelService
from ogame_bot.scheduler.runtime import BotRuntime
from ogame_bot import app

ROOT = Path(__file__).resolve().parents[1]


class InstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.yaml"
        self.path.write_bytes((ROOT / "config.example.yaml").read_bytes())
        self.config = load_config(self.path)
        self.session = MagicMock()
        self.session.state = SessionState.READY
        self.session.before_navigation = None
        self.journal = MagicMock()
        self.journal.pending.return_value = None
        self.runtime = BotRuntime(self.config, self.session, self.journal, None)

    def edit(self) -> None:
        raw = yaml.safe_load(self.path.read_bytes())
        raw["builder"]["reserve"]["metal"] = 777
        raw["builder"]["priorities"] = [{"item": "crystal_mine", "target_level": 15}]
        raw["builder"]["strategy"] = "priority"
        raw["fleet"]["enabled"] = False
        self.path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    def test_reload_waits_for_transaction_boundary_and_updates_all_consumers(self) -> None:
        self.edit()
        self.runtime.scheduler.stack.append(MagicMock())
        self.runtime._reload_instructions()
        self.assertNotEqual(self.runtime.config.brain.reserve["metal"], 777)
        self.runtime.scheduler.stack.clear()
        self.runtime._reload_instructions()
        self.assertEqual(self.runtime.config.brain.reserve["metal"], 777)
        self.assertEqual(self.runtime.brain.planner.settings.build_goals[0].item, "crystal_mine")
        self.assertEqual(self.runtime.upgrades.config.brain.reserve["metal"], 777)
        self.assertEqual(self.session.config.brain.reserve["metal"], 777)
        self.assertNotIn("fleet", self.runtime.scheduler.jobs)
        report = json.loads(self.config.scheduler.status_file.with_name("config_status.json").read_text())
        self.assertEqual(report["state"], "applied")
        self.assertEqual(report["version"], config_version(self.runtime.config))

    def test_bad_or_identity_changes_do_not_replace_working_configuration(self) -> None:
        original = self.runtime.config
        raw = yaml.safe_load(self.path.read_bytes())
        raw["account"]["expected_player_name"] = "Different Commander"
        self.path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        self.runtime._reload_instructions()
        self.assertIs(self.runtime.config, original)
        report = json.loads(self.config.scheduler.status_file.with_name("config_status.json").read_text())
        self.assertEqual(report["state"], "rejected")

    def test_mode_change_requests_read_even_with_research_pending(self) -> None:
        self.runtime.latest = MagicMock()
        self.runtime._read_requested = False
        self.journal.pending.return_value = ('research-order', MagicMock())
        self.edit()
        self.runtime._reload_instructions()
        self.assertTrue(self.runtime._read_requested)
        self.assertEqual(self.runtime.config.brain.strategy, 'priority')

    def test_startup_with_newer_saved_file_does_not_falsely_acknowledge_old_values(self) -> None:
        self.edit()
        runtime = BotRuntime(self.config, self.session, self.journal, None)
        report_path = self.config.scheduler.status_file.with_name("config_status.json")
        self.assertNotEqual(json.loads(report_path.read_text())["version"], config_version(self.config))
        runtime._reload_instructions()
        self.assertEqual(runtime.config.brain.reserve["metal"], 777)

    def test_crlf_config_revision_matches_panel_file_hash(self) -> None:
        data = self.path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.path.write_bytes(data)
        self.assertEqual(load_config(self.path).source_version, hashlib.sha256(data).hexdigest())


class StopMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_startup_cancels_worker_and_runs_cleanup(self) -> None:
        config = load_config(ROOT / "config.example.yaml")
        cleaned = asyncio.Event()

        async def worker(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch("ogame_bot.app._run", side_effect=worker), patch("ogame_bot.app.panel_stop_requested", return_value=True):
            await app.run(config)
        self.assertTrue(cleaned.is_set())

    async def test_stop_waits_until_transaction_finishes(self) -> None:
        config = load_config(ROOT / "config.example.yaml")
        session = MagicMock(transaction_active=True)
        cleaned = asyncio.Event()
        transaction_finished = asyncio.Event()

        async def worker(*args, observer, **kwargs):
            observer(session)
            try:
                await asyncio.sleep(0.3)
                session.transaction_active = False
                transaction_finished.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with patch("ogame_bot.app._run", side_effect=worker), patch("ogame_bot.app.panel_stop_requested", return_value=True):
            await app.run(config)
        self.assertTrue(transaction_finished.is_set())
        self.assertTrue(cleaned.is_set())
