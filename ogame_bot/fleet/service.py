"""Evaluación de flotas; un único envío por lectura y ningún reintento ambiguo."""
from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from ..domain.models import AccountSnapshot, utc_now
from ..storage.orders import OrderJournal
from ..storage.snapshots import save_snapshot
from ..errors import ReadError
from .executor import FleetExecutor, real_flight_enabled
from .planner import FleetPlanner
from .reader import FleetReader
from .fleetsave import plan_fleetsave

logger = logging.getLogger(__name__)


class FleetManager:
    def __init__(self, config: Config, reader: FleetReader, executor: FleetExecutor, journal: OrderJournal) -> None:
        self.config, self.reader, self.executor, self.journal = config, reader, executor, journal
        self.planner = FleetPlanner(config.fleet, config.scanner_max_reading_age_seconds)

    async def process(self, snapshot: AccountSnapshot) -> dict[str, Any]:
        await self.reader.enrich(snapshot)
        save_snapshot(self.config.scanner_output_file, snapshot.to_dict())
        actions = self.planner.plan(snapshot)
        save = self.config.fleet.fleetsave
        if save["enabled"]:
            if self.journal.fleetsave_completed(save["plan_id"]):
                self.planner.notes.append({"reason":"fleetsave_already_confirmed","plan_id":save["plan_id"]})
            elif actions or (snapshot.movements.is_fresh(self.config.scanner_max_reading_age_seconds) and (snapshot.movements.data or {}).get("threat_status") == "clear" and snapshot.fleet_slots.is_fresh(self.config.scanner_max_reading_age_seconds) and snapshot.outgoing.status == "complete" and snapshot.outgoing.is_fresh(self.config.scanner_max_reading_age_seconds)):
                flight, reason = plan_fleetsave(snapshot,self.config.fleet,self.config.brain.resource_weights,self.config.scanner_max_reading_age_seconds)
                self.planner.notes.append({"reason":reason,"plan_id":save["plan_id"]})
                slots = snapshot.fleet_slots.data or {}
                if flight is not None and slots.get("fleet_total",0)-slots.get("fleet_used",0)>0:
                    actions.insert(0,flight)
        occupied = {action.body_id for _, action in self.journal.pending_upgrades() if action.kind == "shipyard"}
        if occupied:
            actions = [action for action in actions if action.origin_id not in occupied]
            self.planner.notes.append({"reason":"shipyard_confirmation_pending","origins":sorted(occupied)})
        report: dict[str, Any] = {"schema_version": 1, "created_at": utc_now(), "dry_run": not real_flight_enabled(actions[0],self.config) if actions else self.config.brain.dry_run,
            "model": "historical_resource_expeditions_with_configured_capacities", "actions": [action.to_dict() for action in actions],
            "notes": self.planner.notes, "result": {"state": "planned"},
            "readings": {"slots": {"status": snapshot.fleet_slots.status, "issues": list(snapshot.fleet_slots.issues)},
                         "outgoing": {"status": snapshot.outgoing.status, "issues": list(snapshot.outgoing.issues)}}}
        save_snapshot(self.config.fleet.plan_file, report)
        if self.journal.pending("fleet") is not None:
            report["result"] = await self.executor.reconcile()
        elif actions:
            try:
                report["result"] = await self.executor.execute(actions[0])
            except ReadError as error:
                # A changed fleet form must not take down construction/defense.
                report["result"] = {"state":"blocked","reason":"fleet_form_unverified","detail":str(error)}
        else:
            report["result"] = {"state": "no_action"}
        save_snapshot(self.config.fleet.plan_file, report)
        logger.info("Flotas: %s propuestas, resultado: %s, simulación: %s; motivos: %s.", len(actions), report["result"]["state"], report["dry_run"] if actions else "sin propuesta", ", ".join(note["reason"] for note in report["notes"]) or "sin bloqueos de planificación")
        return report
