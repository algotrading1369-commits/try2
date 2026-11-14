"""Plan completo revisable y como máximo un envío por evaluación."""
from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from ..core.action_executor import ActionExecutor, uses_real_resources
from ..domain.models import AccountSnapshot, utc_now
from ..storage.orders import OrderJournal
from ..storage.snapshots import save_snapshot
from .planner import Planner, Upgrade
from .settings import effective_brain_settings

logger = logging.getLogger(__name__)


class Brain:
    def __init__(self, config: Config, executor: ActionExecutor, journal: OrderJournal) -> None:
        self.config = config
        self.executor = executor
        self.journal = journal
        self.planner = Planner(config.brain, config.scanner_max_reading_age_seconds)
        self._probe_completion_announced = False

    def select(self, actions: list[Upgrade]) -> Upgrade | None:
        if not actions:
            return None
        energy = next((action for action in actions if action.reason == "energy_deficit"), None)
        if energy is not None:
            return energy
        last = self.journal.last_completed_kind()
        return next((action for action in actions if action.kind != last), actions[0])

    async def process(self, snapshot: AccountSnapshot) -> dict[str, Any]:
        completed = bool(self.config.brain.probe_body_id and self.journal.probe_completed(self.config.brain.probe_body_id))
        if self.config.brain.probe_target and completed:
            if not self._probe_completion_announced:
                logger.info("SONDA COMPLETADA Y CONFIRMADA: continúa la cola normal de edificios e investigación; no se fabricarán más sondas.")
                self._probe_completion_announced = True
        settings = effective_brain_settings(self.config.brain, probe_completed=completed)
        self.planner.settings = settings
        actions = self.planner.plan(snapshot)
        available = [action for action in actions if self.journal.pending_for(action) is None]
        selected = self.select(available)
        report: dict[str, Any] = {
            "schema_version": 1, "created_at": utc_now(), "dry_run": not uses_real_resources(selected, settings) if selected is not None else settings.dry_run and not (settings.probe_bootstrap_live if settings.probe_target else settings.economic_live),
            "cost_model": "classic_without_discounts", "roi_model": "estimated_full_production",
            "actions": [action.to_dict() for action in actions], "notes": self.planner.notes,
            "selected": selected.to_dict() if selected else None,
            "ready_queues": len(available),
            "result": {"state": "planned"},
        }
        report["probe_bootstrap_live"] = settings.probe_bootstrap_live
        report["probe_bootstrap_completed"] = completed
        report["automation_mode"] = "probe" if settings.probe_target else "routine"
        # Una propuesta queda guardada incluso si el proceso se interrumpe al ejecutar.
        save_snapshot(self.config.brain.plan_file, report)
        if selected is None:
            report["result"] = {"state": "no_action", "pending_orders": len(self.journal.pending_upgrades())}
        else:
            report["result"] = await self.executor.execute(selected)
        save_snapshot(self.config.brain.plan_file, report)
        logger.info("Brain: modo %s, %s propuestas, resultado: %s, simulación: %s, siguiente acción: %s, motivos: %s.", report["automation_mode"], len(actions), report["result"]["state"], report["dry_run"], selected.item if selected else "ninguna", ", ".join(note["reason"] for note in self.planner.notes) or "sin bloqueos de planificación")
        return report
