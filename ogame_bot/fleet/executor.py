"""Formulario de tres pasos con comprobación final y envío registrado una sola vez."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from playwright.async_api import Page, Locator
import re

from ..config import Config
from ..core.session import SessionManager
from ..errors import ReadError, IdentityMismatch, ConnectionLost
from ..scanner.dom import Document
from ..scanner import parsing as state
from ..storage.orders import OrderJournal
from . import parsing
from .models import Flight, SHIP_IDS, coordinates
from .reader import FleetReader
from ..adapters.ogame.reader import expand_event_panel
from ..defender.threats import escape_guard


def identity(document: Document, flight: Flight, config: Config, *, require_origin: bool = True) -> None:
    player = document.by_id("playerName")
    if len(player) != 1 or player[0].text != config.expected_player_name:
        raise IdentityMismatch("La cuenta cambió durante el envío de flota.")
    if require_origin and state.current_body_id(document) != flight.origin_id:
        raise ReadError("El cuerpo de origen no coincide.")
    registry = state.body_registry(document, config.server_origin)
    if not any(body.id == flight.origin_id and body.coordinates == flight.origin_coordinates and (flight.purpose != "defender" or (3 if body.kind == "moon" else 1) == flight.origin_type) for body in registry.data or []):
        raise ReadError("El origen no aparece en la lista de cuerpos propios.")
    if flight.mission in {3, 4} and not any(body.id == flight.destination_id and body.coordinates == flight.destination and (3 if body.kind == "moon" else 1) == flight.destination_type for body in registry.data or []):
        raise ReadError("El destino de transporte no es un cuerpo propio verificado.")


def matching_flight(flight: Flight, rows: list[dict[str, Any]]) -> str | None:
    matches = [row["id"] for row in rows if row["id"] not in flight.baseline_ids and not row["returning"] and row["mission"] == flight.mission and row["origin"] == flight.origin_coordinates and row["destination"] == flight.destination and row["destination_type"] == flight.destination_type and row["ships"] == flight.ships and row["resources"] == flight.resources]
    return matches[0] if len(matches) == 1 else None


class FleetExecutor:
    def __init__(self, session: SessionManager, config: Config, journal: OrderJournal) -> None:
        self.session, self.config, self.journal = session, config, journal

    async def reconcile(self) -> dict[str, Any]:
        pending = self.journal.pending("fleet")
        if pending is None:
            return {"state": "no_pending_order"}
        identifier, flight = pending
        if not isinstance(flight, Flight):
            return {"state": "pending", "reason": "upgrade_order_pending"}
        try:
            async with self.session.read_page("movement") as page:
                document = Document(await page.content())
                identity(document, flight, self.config, require_origin=False)
                reading = parsing.outgoing(document)
                match = matching_flight(flight, reading.data or []) if reading.status == "complete" else None
                if match is not None:
                    self.journal.mark(identifier, "confirmed")
                    return {"state": "confirmed", "order_id": identifier, "fleet_id": match}
        except (ReadError, ConnectionLost):
            pass
        return {"state": "pending", "order_id": identifier, "reason": "outgoing_fleet_not_confirmed"}

    async def _single(self, page: Page, name: str, **parameters: Any) -> Locator:
        selector = self.config.fleet.selectors[name].format(**parameters)
        locator = page.locator(selector)
        if await locator.count() != 1:
            raise ReadError("Selector de flota ausente o ambiguo: " + name)
        return locator

    def _authorized(self, flight: Flight) -> bool:
        if flight.purpose == "defender":
            settings = self.config.defender
            return settings.enabled and flight.mission == settings.mission and flight.speed_percent == settings.speed_percent and flight.destination_id in settings.destinations and (not settings.origins or flight.origin_id in settings.origins) and flight.minimum_duration_seconds >= settings.minimum_flight_seconds
        settings = self.config.fleet
        if not settings.enabled or flight.speed_percent != settings.speed_percent:
            return False
        if flight.mission == 15:
            section = settings.expeditions
            return section["enabled"] and flight.origin_id in section["origins"] and flight.holding_hours == section["holding_hours"]
        if flight.mission == 3:
            section = settings.transports
            return section["enabled"] and flight.origin_id in section["sources"] and flight.destination_id == section["destination_id"] and set(flight.ships) == {section["cargo_ship"]}
        section = settings.recycling
        g, s, _ = coordinates(flight.origin_coordinates)
        tg, ts, _ = coordinates(flight.destination)
        return section["enabled"] and flight.origin_id in section["origins"] and g == tg and abs(s - ts) <= section["radius_systems"]

    async def execute(self, flight: Flight) -> dict[str, Any]:
        flight.validate()
        if self.journal.pending("fleet") is not None:
            return await self.reconcile()
        if not self._authorized(flight):
            return {"state": "blocked", "reason": "mission_outside_configuration"}
        defensive = flight.purpose == "defender"
        if not defensive and self.session.defense_paused is True:
            return {"state": "blocked", "reason": "defender_paused_routine_actions"}
        if self.config.brain.dry_run:
            return {"state": "simulated", "action": flight.to_dict()}
        if flight.mission == 8:
            galaxy, system, _ = coordinates(flight.destination)
            field = await FleetReader(self.session, self.config).galaxy(galaxy, system)
            if not any(row["coordinates"] == flight.destination and row["metal"] + row["crystal"] >= self.config.fleet.recycling["minimum_debris"] for row in field.data or []):
                return {"state": "blocked", "reason": "debris_no_longer_verified"}
        # Una lista anterior desconocida no permite identificar un envío nuevo.
        async with self.session.read_page("movement") as page:
            document = Document(await page.content())
            identity(document, flight, self.config, require_origin=False)
            previous = parsing.outgoing(document)
            if previous.status != "complete":
                return {"state": "blocked", "reason": "outgoing_baseline_unknown"}
            flight = replace(flight, baseline_ids=tuple(row["id"] for row in previous.data or []))
        async with self.session.read_page("fleetdispatch", flight.origin_id) as page:
            await page.locator(".technology[data-technology]").first.wait_for(state="attached")
            if defensive:
                await expand_event_panel(page)
            document = Document(await page.content())
            identity(document, flight, self.config)
            available = state.technologies(document, {SHIP_IDS[ship]: ship for ship in flight.ships}, fleet=True)
            values = state.resources(document).data or {}
            counters = parsing.slots(document)
            limits = counters.data or {}
            threat = state.movements(document)
            threat_reason = escape_guard(document, flight, self.config.defender) if defensive else None if (threat.data or {}).get("threat_status") == "clear" else "attacks_unknown_or_detected"
            reserved_slots = 0 if defensive else self.config.fleet.reserved_slots
            verified_slots = all(type(limits.get(key)) is int for key in ("fleet_total", "fleet_used"))
            if not verified_slots or (not defensive and counters.status != "complete") or limits["fleet_total"] - limits["fleet_used"] <= reserved_slots or threat_reason is not None:
                return {"state": "blocked", "reason": "slots_or_attack_status_changed"}
            if flight.mission == 15 and (limits["expedition_used"] >= limits["expedition_total"] or limits["expedition_used"] >= self.config.fleet.expeditions["max_parallel"]):
                return {"state": "blocked", "reason": "expedition_slots_exhausted"}
            if any((available.data or {}).get(ship, -1) < count for ship, count in flight.ships.items()):
                return {"state": "blocked", "reason": "ships_changed"}
            if defensive:
                all_ships = state.technologies(document, {identifier: name for name, identifier in SHIP_IDS.items()}, fleet=True)
                unknown_cards = document.root.find(lambda node: node.has_class("technology") and node.attrs.get("data-technology") not in {*(str(identifier) for identifier in SHIP_IDS.values()), "212", "217"})
                if unknown_cards or all_ships.status != "complete" or {ship: count for ship, count in (all_ships.data or {}).items() if count > 0} != flight.ships:
                    return {"state": "blocked", "reason": "movable_inventory_changed_or_incomplete"}
            # Una selección restaurada por el navegador no puede añadir naves inadvertidas.
            for node in document.root.find(lambda child: child.tag == "input" and bool(re.fullmatch(r"am[0-9]+", child.attrs.get("name", "")))):
                input_name = node.attrs["name"]
                control = page.locator(f'input[name="{input_name}"]')
                if await control.count() != 1:
                    raise ReadError("Campos de nave ambiguos.")
                if await control.is_enabled():
                    await control.fill("0")
                elif await control.input_value() not in {"", "0"}:
                    raise ReadError("Existe una selección de nave deshabilitada no verificable.")
            for ship, count in flight.ships.items():
                await (await self._single(page, "ship_input", id=SHIP_IDS[ship])).fill(str(count))
            await (await self._single(page, "continue_2")).click()
            galaxy, system, position = coordinates(flight.destination)
            for name, value in (("galaxy", galaxy), ("system", system), ("position", position)):
                await (await self._single(page, name)).fill(str(value))
            await (await self._single(page, "target_type")).select_option(str(flight.destination_type))
            # Este contrato usa opciones 10,20,...,100, no unidades implícitas de otra versión.
            await (await self._single(page, "speed")).select_option(str(flight.speed_percent))
            await (await self._single(page, "continue_3")).click()
            await (await self._single(page, "mission", mission=flight.mission)).check()
            if flight.mission == 15:
                await (await self._single(page, "holding")).select_option(str(flight.holding_hours))
            for resource, amount in flight.resources.items():
                await (await self._single(page, "resource_input", resource=resource)).fill(str(amount))
            # Lectura de vuelta: no se confía solo en que fill/select no lanzaran error.
            for ship, count in flight.ships.items():
                if await (await self._single(page, "ship_input", id=SHIP_IDS[ship])).input_value() != str(count):
                    raise ReadError("La composición del formulario no coincide.")
            for name, value in (("galaxy", galaxy), ("system", system), ("position", position), ("target_type", flight.destination_type), ("speed", flight.speed_percent)):
                if await (await self._single(page, name)).input_value() != str(value):
                    raise ReadError("El destino o la velocidad del formulario no coinciden.")
            if not await (await self._single(page, "mission", mission=flight.mission)).is_checked():
                raise ReadError("La misión seleccionada no coincide.")
            if flight.mission == 15 and await (await self._single(page, "holding")).input_value() != str(flight.holding_hours):
                raise ReadError("La permanencia de expedición no coincide.")
            for resource, amount in flight.resources.items():
                if await (await self._single(page, "resource_input", resource=resource)).input_value() != str(amount):
                    raise ReadError("La carga del formulario no coincide.")
            fuel = state.parse_integer(await (await self._single(page, "fuel")).inner_text())
            capacity = state.parse_integer(await (await self._single(page, "capacity")).inner_text())
            duration = state.parse_integer(await (await self._single(page, "flight_seconds")).get_attribute("data-seconds") or "")
            fuel_limit = self.config.defender.maximum_fuel if defensive else self.config.fleet.maximum_fuel
            duration_limit = self.config.defender.maximum_flight_seconds if defensive else self.config.fleet.maximum_oneway_seconds
            if fuel is None or capacity is None or duration is None or capacity <= 0 or duration <= 0 or duration < flight.minimum_duration_seconds or fuel > fuel_limit or duration > duration_limit or (not defensive and sum(flight.resources.values()) + fuel > capacity):
                return {"state": "blocked", "reason": "fuel_capacity_or_duration_unverified"}
            if defensive:
                await expand_event_panel(page)
            final_document = Document(await page.content())
            identity(final_document, flight, self.config)
            if defensive:
                actual_resources = state.resources(final_document).data or {}
                if not all(type(actual_resources.get(key)) is int and actual_resources[key] >= 0 for key in flight.resources) or actual_resources.get("deuterium", -1) < fuel or capacity < fuel:
                    return {"state": "blocked", "reason": "escape_resources_unverified"}
                available_load = {key: actual_resources[key] - (fuel if key == "deuterium" else 0) for key in flight.resources}
                remaining_capacity = capacity - fuel
                if self.config.defender.resource_policy == "require_all" and sum(available_load.values()) > remaining_capacity:
                    return {"state": "blocked", "reason": "cannot_carry_all_resources"}
                load = {key: 0 for key in flight.resources}
                for key in sorted(load, key=lambda key: self.config.brain.resource_weights[key], reverse=True):
                    load[key] = min(remaining_capacity, available_load[key])
                    remaining_capacity -= load[key]
                flight = replace(flight, resources=load)
                for resource, amount in load.items():
                    control = await self._single(page, "resource_input", resource=resource)
                    await control.fill(str(amount))
                    if await control.input_value() != str(amount):
                        raise ReadError("La carga defensiva final no coincide.")
                if state.parse_integer(await (await self._single(page, "fuel")).inner_text()) != fuel or state.parse_integer(await (await self._single(page, "capacity")).inner_text()) != capacity or state.parse_integer(await (await self._single(page, "flight_seconds")).get_attribute("data-seconds") or "") != duration:
                    return {"state": "blocked", "reason": "flight_quote_changed_after_loading"}
                final_document = Document(await page.content())
                identity(final_document, flight, self.config)
            expected_ships = {SHIP_IDS[name]: count for name, count in flight.ships.items()}
            for node in final_document.root.find(lambda child: child.tag == "input" and bool(re.fullmatch(r"am[0-9]+", child.attrs.get("name", "")))):
                input_name = node.attrs["name"]
                expected = expected_ships.get(int(input_name[2:]), 0)
                actual = await page.locator(f'input[name="{input_name}"]').input_value()
                if actual != str(expected) and not (expected == 0 and actual == ""):
                    raise ReadError("El formulario contiene una nave no prevista.")
            values = state.resources(final_document).data or {}
            reserves = self.config.fleet.transports["leave_resources"] if flight.mission == 3 and not defensive else {"metal": 0, "crystal": 0, "deuterium": 0}
            fuel_reserve = 0 if defensive else self.config.fleet.deuterium_reserve
            if any(values.get(key, -1) < amount + max(reserves[key], fuel_reserve if key == "deuterium" else 0) + (fuel if key == "deuterium" else 0) for key, amount in flight.resources.items()):
                return {"state": "blocked", "reason": "resources_or_fuel_reserve_changed"}
            final_threat_reason = escape_guard(final_document, flight, self.config.defender) if defensive else None if (state.movements(final_document).data or {}).get("threat_status") == "clear" else "attack_status_changed_before_send"
            if final_threat_reason is not None or (not defensive and self.session.defense_paused is True):
                return {"state": "blocked", "reason": "attack_status_changed_before_send"}
            sender = await self._single(page, "send")
            if not await sender.is_visible() or not await sender.is_enabled():
                return {"state": "blocked", "reason": "send_button_unavailable"}
            identifier = self.journal.prepare(flight)
            try:
                await sender.click(timeout=self.config.action_timeout_seconds * 1000)
                self.journal.mark(identifier, "submitted")
            except BaseException:
                self.journal.mark(identifier, "uncertain")
                raise
        result = await self.reconcile()
        if defensive:
            result["effective_action"] = flight.to_dict()
        return result
