"""Slots observados y destinos defensivos sin envíos ni datos de cuentas."""
from __future__ import annotations

import unittest
from dataclasses import replace

import test_defender as fixtures
from ogame_bot.defender.planner import destination_options
from ogame_bot.domain.models import CelestialBody, Reading
from ogame_bot.fleet.parsing import slots
from ogame_bot.scanner.dom import Document


class SlotDOMTests(unittest.TestCase):
    def test_span_counters_and_duplicate_tactical_slots_id(self) -> None:
        reading = slots(Document('<div id="slots">Flotas:<span>0/3</span> Expediciones:<span>0/0</span></div><div id="slots">Retirada táctica: 5:1 3:1</div>'))
        self.assertEqual(reading.status, "complete")
        self.assertEqual(reading.data, {"fleet_used": 0, "fleet_total": 3, "expedition_used": 0, "expedition_total": 0})

    def test_duplicated_fleet_label_is_not_trusted(self) -> None:
        reading = slots(Document('<div id="slots">Flotas:0/3 Flotas:1/3 Expediciones:0/1</div>'))
        self.assertNotIn("fleet_used", reading.data)
        self.assertIn("fleet_slots_unknown", reading.issues)

    def test_used_above_total_is_not_trusted(self) -> None:
        reading = slots(Document('<div id="slots">Flotas:4/3 Expediciones:0/1</div>'))
        self.assertNotIn("fleet_used", reading.data)

    def test_missing_labels_do_not_mean_zero(self) -> None:
        self.assertIsNone(slots(Document('<div id="slots">Retirada táctica:5:1</div>')).data)

    def test_full_slots_are_valid_counts(self) -> None:
        reading = slots(Document('<div id="slots">Flotas:3/3 Expediciones:1/1</div>'))
        self.assertEqual(reading.data["fleet_used"], reading.data["fleet_total"])


class OwnDestinationTests(unittest.TestCase):
    def test_single_body_is_explicit_even_without_configuration(self) -> None:
        state = fixtures.threatened()
        state.registry.data[:] = [state.bodies[0].body]
        candidates, reason = destination_options(state, 101, replace(fixtures.config().defender, destinations=()))
        self.assertEqual((candidates, reason), ([], "no_other_own_body"))

    def test_empty_destination_configuration_is_not_automatic_selection(self) -> None:
        candidates, reason = destination_options(fixtures.threatened(), 101, replace(fixtures.config().defender, destinations=()))
        self.assertEqual((candidates, reason), ([], "destinations_not_configured"))

    def test_foreign_or_origin_ids_are_not_destinations(self) -> None:
        for identifiers in ((999,), (101,)):
            candidates, reason = destination_options(fixtures.threatened(), 101, replace(fixtures.config().defender, destinations=identifiers))
            self.assertEqual(candidates, [])
            self.assertEqual(reason, "no_verified_own_destination_outside_known_threats")

    def test_known_attacked_destination_is_rejected(self) -> None:
        state = fixtures.threatened()
        destination = next(body for body in state.registry.data if body.id == 102)
        state.movements.data["events"].append({"possible_incoming_attack": True, "destination": destination.coordinates, "destination_id": 102, "destination_type": 1 if destination.kind == "planet" else 3})
        self.assertEqual(destination_options(state, 101, fixtures.config().defender)[0], [])

    def test_ambiguous_target_excludes_both_planet_and_moon(self) -> None:
        state = fixtures.threatened()
        target = next(body for body in state.registry.data if body.id == 102)
        state.registry.data.append(CelestialBody(104, target.coordinates, "moon" if target.kind == "planet" else "planet", "Otro"))
        state.movements.data["events"].append({"possible_incoming_attack": True, "destination": target.coordinates})
        self.assertEqual(destination_options(state, 101, replace(fixtures.config().defender, destinations=(102, 104)))[0], [])

    def test_unverified_threats_do_not_allow_destination_choice(self) -> None:
        state = fixtures.threatened()
        state.movements = Reading.unavailable("loading")
        self.assertEqual(destination_options(state, 101, fixtures.config().defender)[1], "destination_threats_unknown_or_expired")
