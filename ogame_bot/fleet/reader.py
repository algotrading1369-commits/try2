"""Enriquece el estado con los datos que necesita el gestor de flotas."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from playwright.async_api import Error

from ..adapters.ogame.reader import OGameReader
from ..config import Config
from ..core.session import SessionManager
from ..domain.models import AccountSnapshot, Reading, utc_now
from ..scanner import parsing as state
from ..errors import ReadError, ConnectionLost
from ..scanner.dom import Document
from . import parsing
from .models import coordinates, SHIP_IDS

logger = logging.getLogger(__name__)


class FleetReader:
    def __init__(self, session: SessionManager, config: Config) -> None:
        self.session = session
        self.config = config
        self._ranking = Reading.unavailable("ranking_not_fetched")
        self._next_ranking_fetch = 0.0

    async def galaxy(self, galaxy: int, system: int) -> Reading:
        try:
            async with self.session.read_page("galaxy", galaxy=galaxy, system=system) as page:
                await page.locator("#galaxyContent").wait_for(state="attached")
                galaxy_input, system_input = page.locator("#galaxy_input"), page.locator("#system_input")
                verified = await galaxy_input.count()==1 and await system_input.count()==1 and await galaxy_input.input_value()==str(galaxy) and await system_input.input_value()==str(system)
                return parsing.debris(Document(await page.content()), galaxy, system,location_verified=verified)
        except (ReadError, ConnectionLost):
            return Reading.unavailable("galaxy_read_failed")

    async def enrich(self, snapshot: AccountSnapshot) -> None:
        if not self.config.fleet.enabled:
            return
        reader = OGameReader(self.session)
        origin = snapshot.bodies[0].body.id if snapshot.bodies else None
        stage = "fleetdispatch"
        try:
            origins = set(self.config.fleet.expeditions["origins"] if self.config.fleet.expeditions["enabled"] else [])
            origins.update(self.config.fleet.transports["sources"] if self.config.fleet.transports["enabled"] else [])
            origins.update(self.config.fleet.recycling["origins"] if self.config.fleet.recycling["enabled"] else [])
            if self.config.fleet.fleetsave["enabled"]:
                origins.add(self.config.fleet.fleetsave["origin_id"])
            for body in snapshot.bodies:
                if body.body.id not in origins and (origins or body.body.id != origin):
                    continue
                fleet = await reader.capture("fleetdispatch", body.body.id, expand_events=True)
                body.ships = state.technologies(fleet, {identifier:name for name,identifier in SHIP_IDS.items()}, fleet=True)
                body.resources = state.resources(fleet)
                snapshot.fleet_slots = parsing.slots(fleet)
                snapshot.movements = state.movements(fleet)
                snapshot.registry = state.body_registry(fleet,self.config.server_origin)
            stage = "movement"
            snapshot.outgoing = parsing.outgoing(await reader.capture("movement"))
        except (ReadError, ConnectionLost) as error:
            logger.warning("Flotas: lectura fallida en %s (%s); no se autoriza un envío con movimientos sin verificar.", stage, type(error).__name__)
            snapshot.fleet_slots = Reading.unavailable("fleet_state_read_failed")
            snapshot.outgoing = Reading.unavailable("fleet_state_read_failed")
        expo = self.config.fleet.expeditions
        snapshot.top1_max_reading_age_seconds = expo["top1_max_age_seconds"]
        if expo["enabled"] and expo.get("composition_mode", "automatic") != "manual" and expo["top1_source"] == "public_api":
            if time.monotonic() >= self._next_ranking_fetch:
                self._next_ranking_fetch = time.monotonic() + 3600
                try:
                    response = await self.session.context.request.get(self.config.server_origin + "/api/highscore.xml?category=1&type=0", timeout=self.config.navigation_timeout_seconds * 1000, max_redirects=0)
                    try:
                        self._ranking = parsing.top1_xml(await response.body(), self.config.community + str(self.config.server_number)) if response.ok else Reading.unavailable("ranking_http_error")
                    finally:
                        await response.dispose()
                except Error:
                    self._ranking = Reading.unavailable("ranking_network_error")
                if self._ranking.data is not None:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(self._ranking.observed_at)).total_seconds()
                    self._next_ranking_fetch = time.monotonic() + max(60, min(3600, 3600 - age))
                else:
                    self._next_ranking_fetch = time.monotonic() + 300
            snapshot.top1 = self._ranking
        elif expo["top1_points"] is not None and expo["top1_observed_at"] is not None:
            # La fecha original se conserva: no se rejuvenece al releer el YAML.
            stamp = datetime.fromisoformat(expo["top1_observed_at"]).isoformat()
            snapshot.top1 = Reading({"points": expo["top1_points"]}, stamp, "complete", ("manual_configuration",))
        recycling = self.config.fleet.recycling
        if not recycling["enabled"]:
            return
        systems: list[tuple[int, int]] = []
        for body in snapshot.bodies:
            if body.body.id not in recycling["origins"]:
                continue
            galaxy, system, _ = coordinates(body.body.coordinates)
            # Se empieza por los sistemas más cercanos cuando hay un límite de lecturas.
            for offset in sorted(range(-recycling["radius_systems"], recycling["radius_systems"] + 1), key=abs):
                target = galaxy, system + offset
                if target[1] >= 1 and target not in systems:
                    systems.append(target)
        readings = [await self.galaxy(galaxy, system) for galaxy, system in systems[:recycling["max_systems_per_cycle"]]]
        values = [field for reading in readings for field in reading.data or []]
        issues = tuple(issue for reading in readings for issue in reading.issues)
        if len(systems) > recycling["max_systems_per_cycle"]:
            issues += ("galaxy_scan_limit_reached",)
        # La fecha corresponde al primer sistema, no al final de un barrido largo.
        snapshot.debris = Reading(values, readings[0].observed_at if readings else utc_now(), "partial" if issues else "complete", issues) if readings else Reading.unavailable("no_recycling_origins")
