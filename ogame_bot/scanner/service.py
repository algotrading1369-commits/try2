"""Coordina lecturas por cuerpo; nunca mezcla inventarios de planetas distintos."""
from __future__ import annotations

import logging
from typing import Any

from ..adapters.ogame.reader import OGameReader
from ..config import Config
from ..domain.models import AccountSnapshot, BodySnapshot, Reading, utc_now
from ..errors import ConnectionLost, ReadError
from ..fleet.parsing import slots
from ..storage.snapshots import save_snapshot
from . import parsing
from .dom import Document
from ..brain.catalog import BUILD

logger = logging.getLogger(__name__)


class Scanner:
    def __init__(self, reader: OGameReader, config: Config) -> None:
        self.reader = reader
        self.config = config

    async def _document(self, component: str, body_id: int | None = None, *, expand_events: bool = False) -> Document | None:
        try:
            return await self.reader.capture(component, body_id, expand_events=expand_events)
        except (ReadError, ConnectionLost):
            logger.warning("Lectura incompleta del componente %s.", component)
            return None

    async def _read_body(self, snapshot: BodySnapshot) -> Reading[dict[str, int]]:
        body_id = snapshot.body.id
        overview = await self._document("overview", body_id)
        snapshot.resources = parsing.resources(overview) if overview else Reading.unavailable("overview_read_failed")
        snapshot.temperature = parsing.planet_temperature(overview) if overview else Reading.unavailable("overview_read_failed")
        if snapshot.body.kind == "planet":
            supplies = await self._document("supplies", body_id)
            snapshot.buildings = parsing.technologies(supplies, parsing.BUILDINGS) if supplies else Reading.unavailable("supplies_read_failed")
            snapshot.construction_queue = parsing.queue_state(supplies) if supplies else Reading.unavailable("queue_read_failed")
        else:
            snapshot.buildings = Reading({}, utc_now(), "not_applicable")
        facilities = await self._document("facilities", body_id)
        catalog = {identifier: name for identifier, name in parsing.FACILITIES.items() if identifier in ({14, 21, 41, 42, 43} if snapshot.body.kind == "moon" else {14, 15, 21, 31, 33, 34, 36, 44, 45})}
        snapshot.facilities = parsing.technologies(facilities, catalog) if facilities else Reading.unavailable("facilities_read_failed")
        fleet = await self._document("fleetdispatch", body_id)
        snapshot.ships = parsing.technologies(fleet, parsing.SHIPS, fleet=True) if fleet else Reading.unavailable("fleet_read_failed")
        return slots(fleet) if fleet else Reading.unavailable("fleet_slots_read_failed")

    async def scan(self) -> AccountSnapshot:
        snapshot = AccountSnapshot(self.config.expected_player_name, self.config.server_origin, utc_now(), max_reading_age_seconds=self.config.scanner_max_reading_age_seconds)
        completed = False
        try:
            overview = await self._document("overview", expand_events=True)
            if overview is None:
                snapshot.issues.append("initial_overview_failed")
                return snapshot
            snapshot.registry = parsing.body_registry(overview, self.config.server_origin)
            snapshot.movements = parsing.movements(overview)
            snapshot.bodies = [BodySnapshot(body) for body in snapshot.registry.data or []]
            if not snapshot.bodies:
                snapshot.issues.append("no_verified_bodies")
                return snapshot
            selected = snapshot.bodies[:self.config.scanner_max_bodies]
            if len(selected) < len(snapshot.bodies):
                snapshot.issues.append("body_scan_limit_reached")
            for index, body in enumerate(selected):
                body_slots = await self._read_body(body)
                if index == 0:
                    snapshot.fleet_slots = body_slots
            planet = next((body for body in selected if body.body.kind == "planet"), None)
            if planet is not None:
                research = await self._document("research", planet.body.id)
                snapshot.research = parsing.technologies(research, parsing.RESEARCH) if research else Reading.unavailable("research_read_failed")
                snapshot.research_queue = parsing.queue_state(research, research=True) if research else Reading.unavailable("research_queue_read_failed")
            # El estado global de ataques se vuelve a leer al final del recorrido.
            final_overview = await self._document("overview", expand_events=True)
            if final_overview is not None:
                snapshot.movements = parsing.movements(final_overview)
                self._update_visible_resources(snapshot, final_overview)
            else:
                snapshot.movements = Reading.unavailable("final_movements_read_failed")
            completed = True
            return snapshot
        finally:
            if not completed:
                snapshot.issues.append("scan_incomplete")
            snapshot.finished_at = utc_now()
            save_snapshot(self.config.scanner_output_file, snapshot.to_dict())
            threat: Any = (snapshot.movements.data or {}).get("threat_status", "unknown")
            if threat == "detected":
                logger.critical("Posible ataque entrante detectado. Consulta los movimientos del juego.")
            elif threat == "unknown":
                logger.warning("El scanner no ha podido confirmar el estado de ataques.")
            logger.info("Instantánea guardada: %s cuerpos registrados, amenazas: %s.", len(snapshot.bodies), threat)

    async def refresh_economy(self, snapshot: AccountSnapshot) -> AccountSnapshot:
        """Re-read economy without repeating registry, hangar and fleet pages."""
        facilities_needed = any(BUILD[goal.item].component == "facilities" for goal in self.config.brain.build_goals)
        planets = [body for body in snapshot.bodies[:self.config.scanner_max_bodies] if body.body.kind == "planet"]
        final_document: Document | None = None
        for body in planets:
            supplies = await self._document("supplies", body.body.id)
            body.resources = parsing.resources(supplies) if supplies else Reading.unavailable("supplies_read_failed")
            body.buildings = parsing.technologies(supplies, parsing.BUILDINGS) if supplies else Reading.unavailable("supplies_read_failed")
            body.construction_queue = parsing.queue_state(supplies) if supplies else Reading.unavailable("queue_read_failed")
            final_document = supplies
            if facilities_needed:
                facilities = await self._document("facilities", body.body.id)
                body.facilities = parsing.technologies(facilities, parsing.FACILITIES) if facilities else Reading.unavailable("facilities_read_failed")
        if planets and self.config.brain.research_enabled:
            final_document = await self._document("research", planets[0].body.id, expand_events=True)
            snapshot.research = parsing.technologies(final_document, parsing.RESEARCH) if final_document else Reading.unavailable("research_read_failed")
            snapshot.research_queue = parsing.queue_state(final_document, research=True) if final_document else Reading.unavailable("research_queue_read_failed")
        snapshot.movements = parsing.movements(final_document) if final_document else Reading.unavailable("economy_movements_read_failed")
        if final_document is not None:
            self._update_visible_resources(snapshot, final_document)
        snapshot.finished_at = utc_now()
        save_snapshot(self.config.scanner_output_file, snapshot.to_dict())
        logger.info("Lectura económica actualizada: %s planetas; sin recorrido de flotas.", len(planets))
        return snapshot

    @staticmethod
    def _update_visible_resources(snapshot: AccountSnapshot, document: Document) -> None:
        body_id = parsing.current_body_id(document)
        body = next((body for body in snapshot.bodies if body.body.id == body_id), None)
        if body is not None:
            body.resources = parsing.resources(document)
