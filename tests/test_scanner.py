"""Casos de lectura, cobertura, aislamiento y persistencia sin una cuenta real."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ogame_bot.adapters.ogame.reader import OGameReader
from ogame_bot.config import load_config
from ogame_bot.errors import ConfigurationError, IdentityMismatch, ReadError
from ogame_bot.domain.models import AccountSnapshot, Reading
from ogame_bot.scanner import parsing
from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.service import Scanner
from ogame_bot.storage.snapshots import save_snapshot

ROOT = Path(__file__).resolve().parents[1]
HTML = (Path(__file__).parent / "fixtures" / "scanner.html").read_text(encoding="utf-8")
ORIGIN = "https://s277-es.ogame.gameforge.com"


class ParsingTests(unittest.TestCase):
    def test_exact_es_integers(self) -> None:
        for text, expected in [("1.234.567", 1234567), ("1\u202f234", 1234), ("0", 0), ("42", 42)]:
            with self.subTest(text=text):
                self.assertEqual(parsing.parse_integer(text), expected)
        for text in ("1,2 M", "1.23", "1,234", "Cargando", "12 34", "10/20", "-1"):
            with self.subTest(text=text):
                self.assertIsNone(parsing.parse_integer(text))
        self.assertEqual(parsing.parse_integer("−250", signed=True), -250)

    def test_resources_include_zero_and_negative_energy(self) -> None:
        reading = parsing.resources(Document(HTML))
        self.assertEqual(reading.status, "complete")
        self.assertEqual(reading.data, {"metal": 1234567, "crystal": 456789, "deuterium": 0, "energy": -250})

    def test_unknown_resource_is_not_zero(self) -> None:
        reading = parsing.resources(Document('<div id="resources_metal">1,2 M</div>'))
        self.assertIsNone(reading.data)
        self.assertEqual(reading.status, "unavailable")

    def test_partial_resource_is_explicit(self) -> None:
        reading = parsing.resources(Document('<div id="metal_box">12</div>'))
        self.assertEqual(reading.status, "partial")
        self.assertEqual(reading.data, {"metal": 12})
        self.assertIn("unreadable_energy", reading.issues)

    def test_duplicate_resource_is_not_trusted(self) -> None:
        reading = parsing.resources(Document('<div id="resources_metal">10</div><div id="resources_metal">20</div>'))
        self.assertIsNone(reading.data)

    def test_level_is_not_upgrade_target(self) -> None:
        reading = parsing.technologies(Document(HTML), {1: "metal", 2: "crystal"})
        self.assertEqual(reading.data, {"metal": 30, "crystal": 26})
        self.assertEqual(reading.status, "complete")

    def test_ships_are_available_count_not_form_quantity(self) -> None:
        reading = parsing.technologies(Document(HTML), {202: "small", 203: "large"}, fleet=True)
        self.assertEqual(reading.data, {"small": 1200, "large": 0})

    def test_missing_and_duplicate_technology_remain_unknown(self) -> None:
        document = Document('<li class="technology" data-technology="1">7</li>' * 2)
        reading = parsing.technologies(document, {1: "metal", 2: "crystal"})
        self.assertIsNone(reading.data)

    def test_planet_and_moon_have_distinct_ids(self) -> None:
        reading = parsing.body_registry(Document(HTML), ORIGIN)
        self.assertEqual(reading.status, "complete")
        self.assertEqual([(body.id, body.coordinates, body.kind) for body in reading.data or []], [(101, "1:200:8", "planet"), (102, "1:200:8", "moon"), (103, "2:50:8", "planet")])

    def test_foreign_server_body_link_is_rejected(self) -> None:
        html = HTML.replace('href="/game/index.php', 'href="https://evil.example/game/index.php')
        reading = parsing.body_registry(Document(html), ORIGIN)
        self.assertIsNone(reading.data)

    def test_current_body_needs_meta_not_query_assumption(self) -> None:
        self.assertEqual(parsing.current_body_id(Document(HTML)), 101)
        self.assertIsNone(parsing.current_body_id(Document("<html></html>")))

    def test_attack_and_returning_expedition_are_distinguished(self) -> None:
        reading = parsing.movements(Document(HTML))
        self.assertEqual(reading.status, "complete")
        self.assertIsNotNone(reading.data)
        events = reading.data["events"]
        self.assertEqual(reading.data["threat_status"], "detected")
        self.assertTrue(events[0]["possible_incoming_attack"])
        self.assertFalse(events[1]["possible_incoming_attack"])
        self.assertEqual(events[0]["destination"], "1:200:8")
        self.assertTrue(events[0]["arrival_at"].endswith("+00:00"))

    def test_empty_eventbox_does_not_mean_no_attack(self) -> None:
        reading = parsing.movements(Document('<div id="eventContent"></div>'))
        self.assertEqual(reading.status, "unavailable")
        self.assertIsNone(reading.data)
        self.assertIn("attack_status_unknown", reading.issues)

    def test_global_clear_indicator_is_partial_without_list(self) -> None:
        reading = parsing.movements(Document('<div id="attack_alert" data-status="noAttack"></div>'))
        self.assertEqual(reading.status, "partial")
        self.assertEqual(reading.data["threat_status"], "clear")

    def test_duplicate_event_id_does_not_hide_attack(self) -> None:
        html = HTML.replace('id="eventRow-2"', 'id="eventRow-1"')
        reading = parsing.movements(Document(html))
        self.assertEqual(reading.data["threat_status"], "detected")
        self.assertEqual(len(reading.data["events"]), 2)
        self.assertEqual(reading.status, "partial")

    def test_hostile_event_without_arrival_keeps_threat(self) -> None:
        html = HTML.replace('data-arrival-time="1805120000"', '')
        reading = parsing.movements(Document(html))
        self.assertEqual(reading.data["threat_status"], "detected")
        self.assertIsNone(reading.data["events"][0]["arrival_at"])
        self.assertEqual(reading.status, "partial")

    def test_unknown_mission_is_not_cleared_by_global_indicator(self) -> None:
        html = HTML.replace('data-status="soon"', 'data-status="noAttack"').replace('data-mission-type="1"', '')
        reading = parsing.movements(Document(html))
        self.assertEqual(reading.data["threat_status"], "unknown")


class StorageTests(unittest.TestCase):
    def test_freshness_expires_and_unknown_is_never_fresh(self) -> None:
        now = datetime.now(timezone.utc)
        reading = Reading({"metal": 10}, now.isoformat(), "complete")
        self.assertTrue(reading.is_fresh(120, now + timedelta(seconds=30)))
        self.assertFalse(reading.is_fresh(120, now + timedelta(seconds=121)))
        self.assertFalse(reading.is_fresh(120, now - timedelta(seconds=1)))
        self.assertFalse(Reading.unavailable("test").is_fresh(120, now))

    def test_snapshot_publishes_expiration_per_section(self) -> None:
        snapshot = AccountSnapshot("Test", ORIGIN, "2026-11-13T00:00:00+00:00", max_reading_age_seconds=60)
        snapshot.research = Reading({"astrophysics": 9}, "2026-11-13T00:00:00+00:00", "partial")
        data = snapshot.to_dict()
        self.assertEqual(data["research"]["valid_until"], "2026-11-13T00:01:00+00:00")
        self.assertIsNone(data["movements"]["valid_until"])

    def test_atomic_write_and_failed_replace_preserve_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_snapshot(path, {"version": 1})
            with patch("ogame_bot.storage.snapshots.os.replace", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    save_snapshot(path, {"version": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1})
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_output_cannot_replace_configuration(self) -> None:
        original = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(original.replace("output_file: runtime/state.json", "output_file: config.yaml"), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def reader(self, html: str) -> OGameReader:
        page = MagicMock()
        page.content = AsyncMock(return_value=html)
        session = MagicMock()
        session.config.expected_player_name = "Test Commander"

        @asynccontextmanager
        async def read_page(*args: object, **kwargs: object):
            yield page

        session.read_page = read_page
        return OGameReader(session)

    async def test_wrong_selected_body_is_rejected(self) -> None:
        with self.assertRaises(ReadError):
            await self.reader(HTML).capture("overview", 102)

    async def test_wrong_identity_in_captured_html_stops(self) -> None:
        with self.assertRaises(IdentityMismatch):
            await self.reader(HTML.replace("Test Commander", "Otra cuenta")).capture("overview", 101)


class ScannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(load_config(ROOT / "config.example.yaml"), scanner_output_file=Path(self.temp.name) / "state.json")
        self.reader = MagicMock()

        async def capture(component: str, body_id: int | None = None, **kwargs: object) -> Document:
            html = HTML.replace('content="101"', f'content="{body_id or 101}"')
            if body_id == 103:
                html = html.replace("1.234.567", "222")
            return Document(html)

        self.reader.capture = AsyncMock(side_effect=capture)

    async def test_scan_isolates_bodies_and_saves_only_normalized_data(self) -> None:
        snapshot = await Scanner(self.reader, self.config).scan()
        self.assertEqual(len(snapshot.bodies), 3)
        self.assertEqual(snapshot.bodies[0].resources.data["metal"], 1234567)
        self.assertEqual(snapshot.bodies[2].resources.data["metal"], 222)
        self.assertEqual(snapshot.bodies[1].buildings.status, "not_applicable")
        serialized = self.config.scanner_output_file.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_VALUE", serialized)
        self.assertNotIn("DO_NOT_PERSIST", serialized)
        self.assertNotIn("href", serialized)
        self.assertIsNotNone(json.loads(serialized)["finished_at"])

    async def test_body_limit_reports_unscanned_bodies(self) -> None:
        config = replace(self.config, scanner_max_bodies=1)
        snapshot = await Scanner(self.reader, config).scan()
        self.assertEqual(len(snapshot.bodies), 3)
        self.assertEqual(snapshot.bodies[1].resources.status, "unavailable")
        self.assertIn("body_scan_limit_reached", snapshot.issues)

    async def test_scan_persists_fleet_slots_without_fleet_manager(self) -> None:
        capture = self.reader.capture.side_effect
        async def with_slots(component: str, body_id: int | None = None, **kwargs: object) -> Document:
            document = await capture(component, body_id, **kwargs)
            if component == "fleetdispatch":
                document.feed('<div id="slots">Flotas:1/3 Expediciones:0/1</div>')
            return document
        self.reader.capture.side_effect = with_slots
        snapshot = await Scanner(self.reader, self.config).scan()
        self.assertEqual(snapshot.fleet_slots.status, "complete")
        self.assertEqual(snapshot.fleet_slots.data["fleet_used"], 1)

    async def test_missing_overview_replaces_previous_state_with_unknown(self) -> None:
        save_snapshot(self.config.scanner_output_file, {"old": "clear"})
        self.reader.capture.side_effect = ReadError("synthetic error")
        snapshot = await Scanner(self.reader, self.config).scan()
        self.assertEqual(snapshot.movements.status, "unavailable")
        self.assertIn("scan_incomplete", snapshot.issues)
        saved = json.loads(self.config.scanner_output_file.read_text(encoding="utf-8"))
        self.assertNotIn("old", saved)

    async def test_wrong_identity_aborts_and_saves_incomplete_snapshot(self) -> None:
        self.reader.capture.side_effect = IdentityMismatch("synthetic mismatch")
        with self.assertRaises(IdentityMismatch):
            await Scanner(self.reader, self.config).scan()
        saved = json.loads(self.config.scanner_output_file.read_text(encoding="utf-8"))
        self.assertIn("scan_incomplete", saved["issues"])


if __name__ == "__main__":
    unittest.main()
