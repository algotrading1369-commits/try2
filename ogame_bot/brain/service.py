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
        if self.config.brain.probe_target and self.journal.probe_completed(self.config.brain.probe_body_id):
            if not self._probe_completion_announced:
                logger.info("SONDA COMPLETADA Y CONFIRMADA: preparación finalizada; no se fabricarán más unidades.")
                self._probe_completion_announced = True
            report = {"schema_version": 1, "created_at": utc_now(), "dry_run": self.config.brain.dry_run,
                      "actions": [], "notes": [{"reason": "probe_bootstrap_already_completed"}],
                      "selected": None, "result": {"state": "completed"}}
            save_snapshot(self.config.brain.plan_file, report)
            return report
        actions = self.planner.plan(snapshot)
        selected = self.select(actions)
        report: dict[str, Any] = {
            "schema_version": 1, "created_at": utc_now(), "dry_run": not uses_real_resources(selected, self.config.brain) if selected is not None else self.config.brain.dry_run,
            "cost_model": "classic_without_discounts", "roi_model": "estimated_full_production",
            "actions": [action.to_dict() for action in actions], "notes": self.planner.notes,
            "selected": selected.to_dict() if selected else None,
            "result": {"state": "planned"},
        }
        report["probe_bootstrap_live"] = self.config.brain.probe_bootstrap_live
        # Una propuesta queda guardada incluso si el proceso se interrumpe al ejecutar.
        save_snapshot(self.config.brain.plan_file, report)
        if self.journal.pending("upgrade") is not None:
            report["result"] = await self.executor.reconcile()
        elif selected is None:
            report["result"] = {"state": "no_action"}
        else:
            report["result"] = await self.executor.execute(selected)
        save_snapshot(self.config.brain.plan_file, report)
        logger.info("Brain: %s propuestas, resultado: %s, simulación: %s, preparación limitada de sonda: %s.", len(actions), report["result"]["state"], report["dry_run"], self.config.brain.probe_target == 1)
        return report
