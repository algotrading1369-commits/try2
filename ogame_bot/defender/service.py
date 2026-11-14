"""Monitor de defensa independiente del recorrido completo del scanner."""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from ..adapters.ogame.reader import OGameReader
from ..config import Config
from ..core.session import SessionManager
from ..domain.models import AccountSnapshot, BodySnapshot, Reading, utc_now
from ..errors import ReadError, ConnectionLost, LoginRequired
from ..fleet.executor import FleetExecutor
from ..fleet.models import Flight, SHIP_IDS
from ..fleet.parsing import slots
from ..observability.telegram import AlertDispatcher
from ..scanner import parsing
from ..storage.orders import OrderJournal
from ..storage.snapshots import save_snapshot
from .planner import EscapePlanner, destination_options
from .threats import analyze

logger = logging.getLogger(__name__)

DESTINATION_MESSAGES = {
    "no_other_own_body": "La cuenta no tiene otro planeta o luna propios para desplegar o transportar.",
    "destinations_not_configured": "Configura al menos un destino propio en defender.destinations.",
    "own_destinations_expired": "No se ha podido verificar la lista actual de planetas y lunas propios.",
    "destination_threats_unknown_or_expired": "No se han podido verificar las amenazas de los destinos.",
    "no_verified_own_destination_outside_known_threats": "Los destinos configurados no son propios, coinciden con el origen o tienen amenazas conocidas.",
}


class Defender:
    def __init__(self, session: SessionManager, config: Config, journal: OrderJournal, alerts: AlertDispatcher) -> None:
        self.session, self.config, self.journal, self.alerts = session, config, journal, alerts
        self.reader = OGameReader(session)
        self.executor = FleetExecutor(session, config, journal)
        self.planner = EscapePlanner(config.defender, config.fleet.capacities, config.brain.resource_weights)
        self.last_state = "unknown"

    async def process(self) -> dict[str, Any]:
        settings = self.config.defender
        report = {"schema_version": 1, "checked_at": utc_now(), "state": "disabled", "dry_run": self.config.brain.dry_run, "paused": False, "threats": [], "action": None, "result": {"state": "no_action"}}
        if not settings.enabled:
            self.session.defense_paused = False
            save_snapshot(settings.report_file, report)
            return report
        try:
            document = await self.reader.capture("overview", expand_events=True)
            snapshot = AccountSnapshot(self.config.expected_player_name, self.config.server_origin, utc_now(), max_reading_age_seconds=settings.max_state_age_seconds)
            snapshot.registry = parsing.body_registry(document, self.config.server_origin)
            snapshot.movements = parsing.movements(document)
            self.last_state, threats = analyze(snapshot.movements, snapshot.registry, settings)
            paused = self.last_state != "clear" if settings.pause_on_unknown else self.last_state == "detected"
            self.session.defense_paused = paused
            report.update({"state": self.last_state, "paused": paused, "threats": [asdict(threat) for threat in threats]})
            if self.last_state == "unknown":
                self.alerts.enqueue("defense:unknown", "DEFENSA: no se puede confirmar el estado de ataques. Revisa el juego.")
            elif self.last_state == "detected" and not threats:
                self.alerts.enqueue("defense:unresolved", "ATAQUE: hay indicador de amenaza, pero faltan los datos para evacuar automáticamente.")
            for threat in threats:
                self.alerts.enqueue(f"incoming:{threat.id}:{threat.impact_at}", f"ATAQUE: cuerpo {threat.body_id or 'sin identificar'}, llegada UTC {threat.impact_at or 'desconocida'}. Estado: {threat.reason}.")
            checks: list[dict[str, Any]] = []
            for threat in threats:
                if threat.body_id is None:
                    continue
                candidates, reason = destination_options(snapshot, threat.body_id, settings)
                checks.append({"threat_id": threat.id, "origin_id": threat.body_id,
                               "destination_ids": [body.id for body in candidates], "reason": reason})
                if reason is not None:
                    self.alerts.enqueue(f"escape-destination:{threat.body_id}:{reason}",
                                        "DEFENSA SIN DESTINO VERIFICADO: " + DESTINATION_MESSAGES[reason])
            report["destination_checks"] = checks
            report["trigger_seconds"] = settings.trigger_seconds
            pending = self.journal.pending("fleet")
            if pending is not None:
                report["result"] = await self.executor.reconcile()
                previous = pending[1]
                if isinstance(previous, Flight) and previous.purpose == "defender" and report["result"]["state"] == "confirmed":
                    self.alerts.enqueue("escape-confirmed:" + pending[0], "EVACUACIÓN CONFIRMADA: se ha identificado la flota saliente. Comprueba su destino y llegada en el juego.")
                elif threats:
                    self.alerts.enqueue("escape-pending:" + pending[0], "DEFENSA BLOQUEADA: existe un envío sin confirmar. No se repetirá ni se borrará el registro automáticamente.")
                return report
            eligible = sorted((threat for threat in threats if threat.eligible), key=lambda threat: threat.seconds_to_impact or 0)
            if not eligible:
                blockers = [check["reason"] for check in checks if check["reason"] is not None]
                if blockers:
                    report["result"] = {"state": "blocked", "reason": blockers[0]}
                elif threats:
                    reason = threats[0].reason
                    report["result"] = {"state": "waiting_window" if reason == "outside_evacuation_window" else "blocked", "reason": reason}
                return report
            threat = eligible[0]
            destination_check = next((check for check in checks if check["threat_id"] == threat.id), None)
            if destination_check is not None and destination_check["reason"] is not None:
                report["result"] = {"state": "blocked", "reason": destination_check["reason"]}
                return report
            body = next((body for body in snapshot.registry.data or [] if body.id == threat.body_id), None)
            if body is None:
                return report
            fleet_document = await self.reader.capture("fleetdispatch", body.id, expand_events=True)
            # La amenaza se relee al consultar el inventario, no se usa un aviso anterior.
            snapshot.movements = parsing.movements(fleet_document)
            snapshot.registry = parsing.body_registry(fleet_document, self.config.server_origin)
            _, current_threats = analyze(snapshot.movements, snapshot.registry, settings)
            threat = next((item for item in current_threats if item.id == threat.id and item.eligible), None)
            if threat is None:
                report["result"] = {"state": "blocked", "reason": "threat_changed_during_inventory_read"}
                return report
            inventory = BodySnapshot(body)
            inventory.resources = parsing.resources(fleet_document)
            inventory.ships = parsing.technologies(fleet_document, {identifier: name for name, identifier in SHIP_IDS.items()}, fleet=True)
            if fleet_document.root.find(lambda node: node.has_class("technology") and node.attrs.get("data-technology") not in {*(str(identifier) for identifier in SHIP_IDS.values()), "212", "217"}):
                inventory.ships = Reading.unavailable("unrecognized_fleet_card")
            snapshot.bodies = [inventory]
            snapshot.fleet_slots = slots(fleet_document)
            flight = self.planner.plan(snapshot, threat)
            if flight is None:
                report["result"] = {"state": "blocked", "reason": self.planner.reason}
                self.alerts.enqueue(f"escape-blocked:{threat.id}:{self.planner.reason}", "NO HAY EVACUACIÓN VERIFICABLE: " + self.planner.reason + ". Revisa la cuenta.")
                return report
            report["action"] = flight.to_dict()
            report["result"] = await self.executor.execute(flight)
            result = report["result"]["state"]
            label = "SIMULACIÓN: no se ha enviado ninguna flota" if result == "simulated" else "EVACUACIÓN CONFIRMADA" if result == "confirmed" else "EVACUACIÓN SIN CONFIRMAR: revisa el juego"
            self.alerts.enqueue(f"escape-result:{threat.id}:{result}", label + f". Origen {flight.origin_coordinates}, destino {flight.destination}.")
            return report
        except (ReadError, ConnectionLost, LoginRequired):
            self.last_state = "unknown"
            self.session.defense_paused = settings.pause_on_unknown
            report.update({"state": "unknown", "paused": self.session.defense_paused, "result": {"state": "read_failed"}})
            self.alerts.enqueue("defense:read-failed", "DEFENSA: no se pudo completar la comprobación. La protección automática no está confirmada.")
            return report
        finally:
            report["finished_at"] = utc_now()
            save_snapshot(settings.report_file, report)
