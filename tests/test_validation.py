"""Diagnóstico previo: un bloqueo de asyncio no se presenta como validación real."""
import json
import subprocess
import unittest
from unittest.mock import patch

from ogame_bot.validation import startup_probe


class StartupProbeTests(unittest.TestCase):
    def test_stalled_local_socket_is_reported_without_raw_trace(self):
        result = subprocess.CompletedProcess([], 1, "", "_fallback_socketpair accept PRIVATE_TRACE_VALUE")
        with patch("ogame_bot.validation.subprocess.run", return_value=result):
            report = startup_probe()
        self.assertFalse(report["ok"])
        self.assertTrue(report["local_socketpair_stalled"])
        self.assertNotIn("PRIVATE_TRACE_VALUE", json.dumps(report))

    def test_external_timeout_is_explicit(self):
        with patch("ogame_bot.validation.subprocess.run", side_effect=subprocess.TimeoutExpired([], 8)):
            report = startup_probe()
        self.assertEqual(report["error_type"], "TimeoutExpired")
        self.assertFalse(report["ok"])

    def test_success_requires_exit_code_and_expected_marker(self):
        result = subprocess.CompletedProcess([], 0, "ASYNCIO_OK", "")
        with patch("ogame_bot.validation.subprocess.run", return_value=result):
            self.assertTrue(startup_probe()["ok"])


if __name__ == "__main__":
    unittest.main()
