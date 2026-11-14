"""Controles del panel: origen local, configuración y propiedad del proceso."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from ogame_bot.config import load_config
from ogame_bot.errors import ConfigurationError
from ogame_bot.core.panel_control import stop_requested, registered_control, register_control, unregister_control
from ogame_bot.core.action_executor import uses_real_resources
from ogame_bot.brain.planner import Upgrade
from ogame_bot.brain.catalog import BUILD, RESEARCH, SHIP_BUILD
from ogame_bot.panel import allowed_request, reusable_panel, LocalPanelServer, handler_for
from ogame_bot.panel_service import PanelError, PanelService


class LocalRequestTests(unittest.TestCase):
    def test_foreign_host_or_origin_is_denied(self) -> None:
        for host, origin in (("attacker.example:8765", None), ("127.0.0.1:8765", "https://attacker.example"), ("localhost:8765", "null")):
            self.assertFalse(allowed_request(host, origin, 8765))

    def test_mutations_require_matching_origin_and_token(self) -> None:
        self.assertTrue(allowed_request("127.0.0.1:8765", "http://127.0.0.1:8765", 8765, supplied_token="test-token", token="test-token"))
        self.assertFalse(allowed_request("127.0.0.1:8765", None, 8765, supplied_token="test-token", token="test-token"))
        self.assertFalse(allowed_request("127.0.0.1:8765", "http://127.0.0.1:8765", 8765, supplied_token="wrong", token="test-token"))


class PanelServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.yaml"
        self.path.write_text((Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        self.service = PanelService(self.path)

    def test_state_without_runtime_files_does_not_claim_connection(self) -> None:
        state = self.service.state()
        self.assertEqual(state["scheduler"]["state"], "unknown")
        self.assertFalse(state["process"]["owned_running"])
        self.assertIsNone(state["snapshot"])
        self.assertNotIn("cookies", state)

    def test_old_panel_build_is_not_reused_for_updated_controls(self) -> None:
        existing = {"application": "ogame-local-panel", "installation": self.service.installation}
        self.assertFalse(reusable_panel(existing, self.service))
        existing["backend_build"] = "old-build"
        self.assertFalse(reusable_panel(existing, self.service))
        existing["backend_build"] = self.service.backend_build
        self.assertTrue(reusable_panel(existing, self.service))

    def test_windows_listener_cannot_share_a_port_with_another_panel(self) -> None:
        with LocalPanelServer(("127.0.0.1", 0), handler_for(self.service)) as first:
            with self.assertRaises(OSError):
                with LocalPanelServer(("127.0.0.1", first.server_port), handler_for(self.service)):
                    self.fail("A second backend shared the listener port")

    def test_valid_edit_is_saved_with_backup(self) -> None:
        original = self.path.read_bytes()
        payload = self.service.settings()
        payload["builder"]["reserve"]["deuterium"] = 123
        self.service.save_settings(payload)
        self.assertEqual(load_config(self.path).brain.reserve["deuterium"], 123)
        backups = list((self.root / "runtime").glob("config-panel-*.yaml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_invalid_or_stale_configuration_preserves_file(self) -> None:
        original = self.path.read_bytes()
        payload = self.service.settings()
        payload["version"] = "outdated"
        with self.assertRaises(PanelError):
            self.service.save_settings(payload)
        self.assertEqual(self.path.read_bytes(), original)
        payload = self.service.settings()
        payload["builder"]["reserve"]["deuterium"] = -1
        with self.assertRaises(ConfigurationError):
            self.service.save_settings(payload)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unknown_fields_and_live_fleet_mode_are_rejected(self) -> None:
        for mutate in (lambda p: p["automation"].update(password="do-not-store"), lambda p: p["automation"].update(dry_run=False)):
            payload = self.service.settings()
            mutate(payload)
            with self.assertRaises(PanelError):
                self.service.save_settings(payload)

    def test_running_process_accepts_instructions_but_prevents_duplicate_start(self) -> None:
        self.service.process = MagicMock(pid=55)
        self.service.process.poll.return_value = None
        payload = self.service.settings()
        payload["builder"]["reserve"]["metal"] = 321
        self.service.save_settings(payload)
        self.assertEqual(load_config(self.path).brain.reserve["metal"], 321)
        with patch("ogame_bot.panel_service.subprocess.Popen") as launch, self.assertRaises(PanelError):
            self.service.start()
        launch.assert_not_called()

    def test_active_browser_changes_require_restart_but_keep_other_settings_intact(self) -> None:
        self.service.process = MagicMock(pid=55)
        self.service.process.poll.return_value = None
        before = self.path.read_bytes()
        payload = self.service.settings()
        payload["browser"]["headless"] = not payload["browser"]["headless"]
        with self.assertRaises(ConfigurationError):
            self.service.save_settings(payload)
        self.assertEqual(self.path.read_bytes(), before)

    def test_start_uses_fixed_module_and_per_process_stop_token(self) -> None:
        with patch("ogame_bot.panel_service.subprocess.Popen") as launch:
            self.service.start()
        args, kwargs = launch.call_args
        self.assertEqual(args[0][1:3], ["-m", "ogame_bot"])
        self.assertEqual(args[0][-1], str(self.path.resolve()))
        self.assertEqual(kwargs["env"]["OGAME_PANEL_CONTROL_TOKEN"], self.service.control_token)
        self.assertNotIn("shell", kwargs)

    def test_stop_only_signals_owned_process_with_matching_token(self) -> None:
        self.service.process = MagicMock(pid=55)
        self.service.process.poll.return_value = None
        self.service.control_token = "local-test-token"
        self.service.stop()
        path = self.root / "runtime" / "panel_stop.json"
        self.assertTrue(stop_requested(path, "local-test-token", 55))
        self.assertFalse(stop_requested(path, "local-test-token", 56))
        self.assertFalse(stop_requested(path, "another-token", 55))
        self.service.process.terminate.assert_not_called()
        self.service.process.kill.assert_not_called()

    def test_unowned_process_is_never_stopped(self) -> None:
        with self.assertRaises(PanelError):
            self.service.stop()

    def test_malformed_stop_file_is_ignored(self) -> None:
        path = self.root / "stop.json"
        path.write_text(json.dumps({"pid": 55, "token": "t", "action": "launch"}), encoding="utf-8")
        self.assertFalse(stop_requested(path, "t", 55))

    def test_all_module_settings_roundtrip_and_paths_remain_private(self) -> None:
        payload = self.service.settings()
        payload["scanner"]["min_interval_seconds"] = 40
        payload["scanner"]["max_interval_seconds"] = 80
        payload["fleet"]["expeditions"]["max_parallel"] = 3
        payload["defender"]["speed_percent"] = 10
        payload["builder"]["priorities"] = [{"item": "metal_mine", "target_level": 12}, {"item": "crystal_mine", "target_level": 10}]
        payload["automation"]["economic_live"] = True
        payload["builder"]["temperatures"] = {"1": 40}
        self.service.save_settings(payload)
        config = load_config(self.path)
        self.assertEqual(config.scanner_min_interval_seconds, 40)
        self.assertEqual(config.fleet.expeditions["max_parallel"], 3)
        self.assertEqual(config.brain.build_goals[0].item, "metal_mine")
        self.assertTrue(config.brain.economic_live)
        self.assertTrue(config.brain.dry_run)
        self.assertEqual(config.brain.temperatures, {1: 40})
        self.assertNotIn("profile_dir", payload["browser"])
        self.assertNotIn("token_env", payload["telegram"])

    def test_invalid_nested_module_settings_preserve_configuration(self) -> None:
        for section, key, value in (("fleet", "speed_percent", 13), ("scanner", "min_interval_seconds", -1), ("builder", "strategy", "unknown")):
            before = self.path.read_bytes()
            payload = self.service.settings()
            payload[section][key] = value
            with self.assertRaises(ConfigurationError):
                self.service.save_settings(payload)
            self.assertEqual(before, self.path.read_bytes())

    def test_registration_survives_panel_restart_and_accepts_cooperative_stop(self) -> None:
        directory = self.service.config.scheduler.status_file.parent
        with patch.dict("os.environ", {}, clear=True):
            token = register_control(directory)
            try:
                another_panel = PanelService(self.path)
                status = another_panel.process_status()
                self.assertTrue(status["can_stop"])
                self.assertTrue(status["external_running"])
                self.assertNotIn("token", json.dumps(status))
                another_panel.stop()
                import os
                self.assertTrue(stop_requested(directory / "panel_stop.json", token, os.getpid()))
                self.assertTrue(another_panel.process_status()["stop_requested"])
                with patch.dict("os.environ", {}, clear=True):
                    replacement = register_control(directory)
                    self.assertFalse(another_panel.process_status()["stop_requested"])
                    unregister_control(directory, replacement)
            finally:
                unregister_control(directory, token)
        self.assertFalse((directory / "bot_control.json").exists())

    def test_stale_registration_does_not_stop_a_reused_pid(self) -> None:
        path = self.service.config.scheduler.status_file.with_name("bot_control.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 55, "birth": "old", "token": "x" * 32}), encoding="utf-8")
        with patch("ogame_bot.core.panel_control.process_identity", return_value="new"):
            self.assertIsNone(registered_control(path))
            with self.assertRaises(PanelError):
                self.service.stop()

    def test_stop_uses_registered_worker_pid_instead_of_windows_launcher(self) -> None:
        self.service.process = MagicMock(pid=55)
        self.service.process.poll.return_value = None
        self.service.control_token = "launcher-token"
        worker = {"pid": 77, "token": "worker-token", "birth": "123"}
        with patch("ogame_bot.panel_service.registered_control", return_value=worker):
            self.assertEqual(self.service.process_status()["pid"], 77)
            self.service.stop()
            self.assertTrue(self.service.process_status()["stop_requested"])
        path = self.service.config.scheduler.status_file.with_name("panel_stop.json")
        self.assertTrue(stop_requested(path, "worker-token", 77))
        self.assertFalse(stop_requested(path, "launcher-token", 55))

    def test_early_stop_is_handed_from_launcher_to_registered_interpreter(self) -> None:
        import os
        directory = self.service.config.scheduler.status_file.parent
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "panel_stop.json"
        token = "one-launch-capability-token"
        path.write_text(json.dumps({"action": "stop", "pid": 123, "token": token}), encoding="utf-8")
        with patch.dict("os.environ", {"OGAME_PANEL_CONTROL_TOKEN": token}):
            register_control(directory)
            try:
                self.assertTrue(stop_requested(path, token, os.getpid()))
            finally:
                unregister_control(directory, token)

    def test_fresh_old_heartbeat_without_profile_lock_does_not_block_start(self) -> None:
        with patch.object(self.service, "scheduler_status", return_value={"state": "running", "heartbeat_is_recent": True}):
            self.assertFalse(self.service.process_status()["external_running"])

    def test_locked_legacy_profile_is_detected_even_without_heartbeat(self) -> None:
        from ogame_bot.core.locking import profile_lock
        with profile_lock(self.service.config.profile_dir):
            self.assertTrue(self.service.process_status()["external_running"])
            self.assertFalse(self.service.process_status()["can_stop"])
            with self.assertRaises(PanelError):
                self.service.start()

    def test_scanner_mode_is_explicit_and_unknown_modes_are_rejected(self) -> None:
        with patch("ogame_bot.panel_service.subprocess.Popen") as launch:
            self.service.start("scanner")
            self.assertIn("--scanner-only", launch.call_args.args[0])
        with self.assertRaises(PanelError):
            self.service.start("untrusted --argument")

    def test_economic_live_scope_excludes_shipyard_and_probe_mode(self) -> None:
        settings = replace(self.service.config.brain, dry_run=True, economic_live=True, probe_target=0, probe_bootstrap_live=False)
        for kind, item, catalog in (("building", "metal_mine", BUILD), ("research", "energy_technology", RESEARCH), ("shipyard", "espionage_probe", SHIP_BUILD)):
            action = Upgrade(kind, item, 1, "1:1:1", 0, 1, catalog[item].cost(1), "test")
            self.assertEqual(uses_real_resources(action, settings), kind != "shipyard")
            self.assertFalse(uses_real_resources(action, replace(settings, economic_live=False)))
            self.assertFalse(uses_real_resources(action, replace(settings, probe_target=1, probe_body_id=1)))
