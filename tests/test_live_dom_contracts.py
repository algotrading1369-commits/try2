"""Regresiones de estructuras observadas en OGame ES 13.1.0-r5.

Fragmentos mínimos sin scripts, credenciales ni información de la cuenta.
"""
from __future__ import annotations

import unittest

from ogame_bot.scanner.dom import Document
from ogame_bot.scanner import parsing


class LiveDOMContracts(unittest.TestCase):
    def test_attack_class_without_data_status(self) -> None:
        reading = parsing.movements(Document('<div id="attack_alert" class="tooltip noAttack"></div>'))
        self.assertEqual(reading.data["threat_status"], "clear")

    def test_attack_class_remains_detected(self) -> None:
        for status in ("soon", "attack"):
            reading = parsing.movements(Document(f'<div id="attack_alert" class="tooltip {status}"></div>'))
            self.assertEqual(reading.data["threat_status"], "detected")

    def test_conflicting_or_missing_markers_are_unknown(self) -> None:
        for attrs in ('class="noAttack attack"', 'class="noAttack" data-status="attack"', 'class="tooltip"'):
            reading = parsing.movements(Document(f'<div id="attack_alert" {attrs}></div>'))
            self.assertNotEqual((reading.data or {}).get("threat_status"), "clear")

    def test_explicit_empty_fleet_is_zero(self) -> None:
        document = Document('<div id="warning"><h3>No se puede enviar la flota.</h3><p><span class="icon icon_warning"></span>No hay naves en este planeta.</p></div>')
        reading = parsing.technologies(document, parsing.SHIPS, fleet=True)
        self.assertEqual(reading.status, "complete")
        self.assertEqual(reading.data, dict.fromkeys(parsing.SHIPS.values(), 0))

    def test_other_warnings_and_conflicting_cards_are_not_zero(self) -> None:
        for html in ('<div id="warning"><p>No se puede enviar la flota.</p></div>', '<div id="warning"><p>No hay naves en este planeta.</p></div><li class="technology" data-technology="202"><span class="amount">7</span></li>'):
            reading = parsing.technologies(Document(html), parsing.SHIPS, fleet=True)
            self.assertNotEqual(reading.status, "complete")

    def test_building_idle_is_scoped_away_from_shipyard(self) -> None:
        document = Document('<div id="productionboxBottom"><div id="productionboxbuildingcomponent"><h3>Edificio</h3><table><tr><td class="idle"><a>No hay edificios en construcción.<br>(A los recursos)</a></td></tr></table></div><div id="productionboxshipyardcomponent"><span class="countDown">10</span></div></div>')
        self.assertEqual(parsing.queue_state(document).data, {"state": "free"})

    def test_building_countdown_beats_idle(self) -> None:
        document = Document('<div id="productionboxBottom"><div id="productionboxbuildingcomponent"><td class="idle">No hay edificios en construcción.</td><span class="countDown">10</span></div></div>')
        self.assertEqual(parsing.queue_state(document).data, {"state": "busy"})

    def test_research_idle(self) -> None:
        document = Document('<div id="productionboxBottom"><div id="productionboxresearchcomponent"><h3>Investigación</h3><table><tr><td class="idle">No hay ninguna investigación en progreso en este momento.</td></tr></table></div></div>')
        self.assertEqual(parsing.queue_state(document, research=True).data, {"state": "free"})
