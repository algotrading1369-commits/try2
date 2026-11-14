"""El diagnóstico no confunde datos antiguos ni simulación con operación real."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ogame_bot.config import load_config
from ogame_bot.fleet.models import SHIP_IDS
from ogame_bot.readiness import diagnose, format_report, load_state


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
        self.config = replace(cfg, defender=replace(cfg.defender, mission=6), fleet=replace(cfg.fleet, capacities={}))
        self.now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        self.snapshot = {
            "player": cfg.expected_player_name, "server": cfg.server_origin,
            "fleet_slots": self.reading({"fleet_used": 0, "fleet_total": 3}),
            "bodies": [{"body": {"id": 101},
                        "ships": self.reading({ship: int(ship == "small_cargo") for ship in SHIP_IDS}),
                        "resources": self.reading({"deuterium": 1000})}],
            "private_extra_field": "DO_NOT_PRINT_PRIVATE_VALUE",
        }

    def reading(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"data": data, "observed_at": self.now.isoformat(), "status": "complete"}

    def test_reports_capacity_probe_and_fuel_blockers(self) -> None:
        report = diagnose(self.config, self.snapshot, self.now)
        codes = {row["code"] for row in report["checks"] if row["state"] == "blocked"}
        self.assertTrue({"ship_capacities", "body_101_probe", "body_101_fuel"} <= codes)
        self.assertNotIn("own_destination", codes)
        self.assertNotIn("DO_NOT_PRINT_PRIVATE_VALUE", format_report(report))

    def test_stale_inventory_cannot_claim_probe_absent(self) -> None:
        self.snapshot["bodies"][0]["ships"]["observed_at"] = (self.now - timedelta(hours=1)).isoformat()
        checks = diagnose(self.config, self.snapshot, self.now)["checks"]
        self.assertTrue(any(row["code"] == "body_101_inventory" and row["state"] == "pending" for row in checks))
        self.assertFalse(any(row["code"] == "body_101_probe" for row in checks))

    def test_foreign_snapshot_is_not_used(self) -> None:
        self.snapshot["player"] = "Otra identidad"
        report = diagnose(self.config, self.snapshot, self.now)
        self.assertTrue(any(row["code"] == "snapshot_identity" for row in report["checks"]))
        self.assertFalse(any(row["code"].startswith("body_") for row in report["checks"]))

    def test_simulation_never_certifies_autonomy(self) -> None:
        report = diagnose(self.config, self.snapshot, self.now)
        self.assertEqual(report["autonomous_operation"], "unverified")
        self.assertTrue(any(row["code"] == "live_fleet_validation" and row["state"] == "pending" for row in report["checks"]))

    def test_missing_or_malformed_snapshot_has_no_claimed_inventory(self) -> None:
        for snapshot in (None, {"player": self.config.expected_player_name, "server": self.config.server_origin, "bodies": [None, {}, {"body": {"id": 101}, "ships": 7}]}):
            report = diagnose(self.config, snapshot, self.now)
            self.assertEqual(report["autonomous_operation"], "unverified")

    def test_empty_priority_goals_are_explicit(self) -> None:
        config = replace(self.config, brain=replace(self.config.brain, strategy="priority", builder_enabled=True, build_goals=()))
        self.assertTrue(any(row["code"] == "building_goals" for row in diagnose(config, None, self.now)["checks"]))

    def test_unreadable_state_does_not_expose_error_contents(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.assertIsNone(load_state(path))
            path.write_text("DO_NOT_PRINT_PRIVATE_VALUE", encoding="utf-8")
            self.assertIsNone(load_state(path))

    def test_exhausted_slot_is_blocked(self) -> None:
        self.snapshot["fleet_slots"] = self.reading({"fleet_used": 3, "fleet_total": 3})
        self.assertTrue(any(row["code"] == "fleet_slots" and row["state"] == "blocked" for row in diagnose(self.config, self.snapshot, self.now)["checks"]))
