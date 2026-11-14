"""Controles del panel: origen local, configuración y propiedad del proceso."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ogame_bot.config import load_config
from ogame_bot.errors import ConfigurationError
from ogame_bot.core.panel_control import stop_requested
from ogame_bot.panel import allowed_request
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

    def test_running_process_prevents_config_edit_and_duplicate_start(self) -> None:
        self.service.process = MagicMock(pid=55)
        self.service.process.poll.return_value = None
        with self.assertRaises(PanelError):
            self.service.save_settings(self.service.settings())
        with patch("ogame_bot.panel_service.subprocess.Popen") as launch, self.assertRaises(PanelError):
            self.service.start()
        launch.assert_not_called()

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
