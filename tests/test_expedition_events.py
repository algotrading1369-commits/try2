"""Expedition events must not pause the economy as unknown attacks."""
from __future__ import annotations

import unittest

from ogame_bot.scanner.dom import Document
from ogame_bot.scanner.parsing import movements


def event(identifier: int, *, mission: int = 15, returning: str = 'false', target: str = '1:200:16', arrival: str = '1794829942') -> str:
    return f'<tr class="eventFleet" id="eventRow-{identifier}" data-mission-type="{mission}" data-return-flight="{returning}" data-arrival-time="{arrival}"><td class="coordsOrigin">[1:200:8]</td><td class="destCoords">[{target}]</td></tr>'


class ExpeditionEventsTests(unittest.TestCase):
    def test_departure_holding_and_return_do_not_pause_clear_account(self):
        html = '<div id="attack_alert" data-status="noAttack"></div>'+event(1)+event(2)+event(3,returning='true')
        reading = movements(Document(html))
        self.assertEqual(reading.data['threat_status'],'clear')
        self.assertTrue(all(row['hostile'] is False for row in reading.data['events']))
        self.assertFalse(any(issue.startswith('incomplete_event') for issue in reading.issues))

    def test_uncertain_attack_or_invalid_expedition_still_blocks(self):
        for row in (event(1,mission=1,target='1:200:8'),event(1,target='1:200:8'),event(1,arrival='Cargando'),event(1,returning='unknown')):
            with self.subTest(row=row):
                reading = movements(Document('<div id="attack_alert" data-status="noAttack"></div>'+row))
                self.assertEqual(reading.data['threat_status'],'unknown')

    def test_global_attack_indicator_always_wins(self):
        reading = movements(Document('<div id="attack_alert" data-status="attack"></div>'+event(1)))
        self.assertEqual(reading.data['threat_status'],'detected')

    def test_missing_global_indicator_is_not_assumed_clear(self):
        self.assertEqual(movements(Document(event(1))).data['threat_status'],'unknown')
