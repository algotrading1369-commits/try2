"""Cambios de cuenta sin mezclar registros ni perder órdenes pendientes."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ogame_bot.config import load_config
from ogame_bot.errors import CoreError
from ogame_bot.storage.orders import OrderJournal
from test_brain import Planner, snapshot

ROOT = Path(__file__).resolve().parents[1]


class AccountJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "orders.sqlite3"
        settings = replace(load_config(ROOT / "config.example.yaml").brain, research_enabled=False)
        self.action = Planner(settings, 120).plan(snapshot())[0]

    def test_new_account_gets_stable_private_namespace(self):
        first = OrderJournal.open_for_account(self.path, "server|first")
        first_path = first.path
        first.prepare(self.action)
        first.close()
        again = OrderJournal.open_for_account(self.path, "server|first")
        self.assertEqual(again.path, first_path)
        self.assertIsNotNone(again.pending())
        again.close()
        other = OrderJournal.open_for_account(self.path, "server|second")
        self.assertNotEqual(other.path, first_path)
        self.assertIsNone(other.pending())
        other.close()
        self.assertFalse(self.path.exists())

    def test_empty_placeholder_legacy_journal_is_preserved(self):
        OrderJournal(self.path, "server|Test Commander").close()
        before = self.path.read_bytes()
        current = OrderJournal.open_for_account(self.path, "server|Actual player")
        self.assertNotEqual(current.path, self.path)
        current.close()
        self.assertEqual(self.path.read_bytes(), before)

    def test_matching_legacy_pending_order_is_reused(self):
        old = OrderJournal(self.path, "server|first")
        identifier = old.prepare(self.action)
        old.close()
        current = OrderJournal.open_for_account(self.path, "server|first")
        self.assertEqual(current.path, self.path)
        self.assertEqual(current.pending()[0], identifier)
        current.close()

    def test_mismatched_legacy_pending_order_is_not_bypassed(self):
        old = OrderJournal(self.path, "server|first")
        old.prepare(self.action)
        old.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(CoreError, "órdenes sin confirmar"):
            OrderJournal.open_for_account(self.path, "server|second")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(list(self.path.parent.glob("*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
