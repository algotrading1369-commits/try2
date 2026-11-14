"""Único punto de envío de mejoras. Persiste antes del clic y no lo reintenta."""
from __future__ import annotations

from typing import Any
import logging

from ..brain.catalog import MINES, energy_delta
from ..brain.settings import BrainSettings, effective_brain_settings
from ..brain.planner import Upgrade, afford, probe_bootstrap_action
from ..config import Config
from ..errors import IdentityMismatch, ReadError, ConnectionLost
from ..scanner.dom import Document
from ..scanner import parsing
from ..storage.orders import OrderJournal
from .session import SessionManager

logger = logging.getLogger(__name__)


def uses_real_resources(upgrade: Upgrade, settings: BrainSettings) -> bool:
    return (not settings.dry_run
            or (settings.probe_bootstrap_live and probe_bootstrap_action(upgrade, settings))
            or (settings.economic_live and not settings.probe_target and upgrade.kind in {"building", "research"}))


def verify_identity(document: Document, upgrade: Upgrade, config: Config) -> None:
    names = document.by_id("playerName")
    if len(names) != 1 or names[0].text != config.expected_player_name:
        raise IdentityMismatch("La cuenta no coincide durante la verificación de la orden.")
    if parsing.current_body_id(document) != upgrade.body_id:
        raise ReadError("El planeta no coincide durante la verificación de la orden.")
    registry = parsing.body_registry(document, config.server_origin)
    if not any(body.id == upgrade.body_id and body.coordinates == upgrade.coordinates and body.kind == "planet" for body in registry.data or []):
        raise ReadError("No se pudo verificar la identidad completa del planeta.")


def current_level(document: Document, upgrade: Upgrade) -> int | None:
    reading = parsing.technologies(document, {upgrade.technology.id: upgrade.item}, fleet=upgrade.kind == "shipyard")
    return (reading.data or {}).get(upgrade.item)


def preflight(document: Document, upgrade: Upgrade, config: Config) -> str | None:
    verify_identity(document, upgrade, config)
    if current_level(document, upgrade) != upgrade.current_level:
        return "level_changed_or_unknown"
    threat = parsing.movements(document)
    if (threat.data or {}).get("threat_status") != "clear":
        return "attacks_detected_or_unknown"
    queue = parsing.queue_state(document, research=upgrade.kind == "research", shipyard=upgrade.kind == "shipyard")
    if (queue.data or {}).get("state") != "free":
        return "queue_busy_or_unknown"
    resources = parsing.resources(document)
    values = resources.data or {}
    if not all(type(values.get(resource)) is int for resource in ("metal", "crystal", "deuterium", "energy")):
        return "resources_unknown"
    if not afford(values, upgrade.technology.cost(upgrade.target_level), config.brain.reserve):
        return "resources_or_reserve_insufficient"
    if upgrade.kind == "building" and upgrade.item != "solar_plant":
        consumption = energy_delta(upgrade.item, upgrade.current_level) if upgrade.item in MINES else 0
        if values["energy"] - consumption < config.brain.minimum_energy:
            return "energy_insufficient"
    return None


class ActionExecutor:
    def __init__(self, session: SessionManager, config: Config, journal: OrderJournal) -> None:
        self.session = session
        self.config = config
        self.journal = journal

    async def reconcile(self) -> dict[str, Any]:
        pending = self.journal.pending("upgrade")
        if pending is None:
            return {"state": "no_pending_order"}
        identifier, upgrade = pending
        if not isinstance(upgrade, Upgrade):
            return {"state": "pending", "order_id": identifier, "reason": "fleet_order_pending"}
        try:
            async with self.session.read_page(upgrade.technology.component, upgrade.body_id) as page:
                await page.locator(".technology[data-technology]").first.wait_for(state="attached")
                document = Document(await page.content())
                verify_identity(document, upgrade, self.config)
                level = current_level(document, upgrade)
                if level is not None and level >= upgrade.target_level:
                    self.journal.mark(identifier, "confirmed")
                    logger.info("Orden confirmada: %s, objetivo %s, planeta %s.", upgrade.technology.name_es, upgrade.target_level, upgrade.body_id)
                    return {"state": "confirmed", "order_id": identifier}
        except (ReadError, ConnectionLost):
            return {"state": "pending", "order_id": identifier, "reason": "reconciliation_unavailable"}
        return {"state": "pending", "order_id": identifier, "reason": "target_level_not_confirmed"}

    async def execute(self, upgrade: Upgrade) -> dict[str, Any]:
        # Valida también llamadas que no provengan directamente del planificador.
        Upgrade.from_dict(upgrade.to_dict())
        if self.journal.pending("upgrade") is not None:
            return await self.reconcile()
        configured = self.config.brain
        completed = bool(configured.probe_body_id and self.journal.probe_completed(configured.probe_body_id))
        if upgrade.kind == "shipyard" and completed:
            return {"state": "completed", "reason": "probe_bootstrap_already_completed"}
        settings = effective_brain_settings(configured, probe_completed=completed)
        bootstrap = probe_bootstrap_action(upgrade, settings)
        if settings.probe_target:
            allowed = bootstrap and (upgrade.kind == "shipyard" or (settings.builder_enabled if upgrade.kind == "building" else settings.research_enabled))
            if self.journal.probe_completed(settings.probe_body_id):
                return {"state": "completed", "reason": "probe_bootstrap_already_completed"}
        elif upgrade.kind == "building":
            allowed = settings.builder_enabled and (
                (upgrade.item == "solar_plant" and settings.auto_solar and upgrade.target_level <= settings.max_solar_level)
                or any(goal.item == upgrade.item and upgrade.target_level <= goal.target_level for goal in settings.build_goals)
            )
        elif upgrade.kind == "research":
            allowed = settings.research_enabled and any(goal.item == upgrade.item and upgrade.target_level <= goal.target_level for goal in settings.research_goals)
        else:
            allowed = False
        if not allowed:
            return {"state": "blocked", "reason": "module_disabled_or_outside_configured_goals"}
        if self.session.defense_paused is True:
            return {"state": "blocked", "reason": "defender_paused_routine_actions"}
        if not uses_real_resources(upgrade, settings):
            return {"state": "simulated", "action": upgrade.to_dict()}
        async with self.session.read_page(upgrade.technology.component, upgrade.body_id) as page:
            await page.locator(".technology[data-technology]").first.wait_for(state="attached")
            await page.locator("#eventboxFilled:visible, #eventboxBlank:visible").first.wait_for(state="visible")
            document = Document(await page.content())
            reason = preflight(document, upgrade, self.config)
            if reason is not None:
                return {"state": "blocked", "reason": reason}
            technology = upgrade.technology
            card = page.locator(f'.technology[data-technology="{technology.id}"]')
            if await card.count() != 1 or await card.get_attribute("aria-label") != technology.name_es:
                return {"state": "blocked", "reason": "technology_card_unrecognized"}
            if upgrade.kind == "shipyard":
                if await card.get_attribute("data-status") != "on":
                    return {"state": "blocked", "reason": "shipyard_requirements_or_resources_unavailable"}
                await card.click()
                amount = page.locator("#technologydetails #build_amount")
                await amount.wait_for(state="visible")
                if await amount.count() != 1:
                    return {"state": "blocked", "reason": "ship_quantity_control_ambiguous"}
                await amount.fill("1")
                if await amount.input_value() != "1":
                    return {"state": "blocked", "reason": "ship_quantity_not_verified"}
                button = page.locator('#technologydetails button.upgrade[data-technology="210"]')
                reason = preflight(Document(await page.content()), upgrade, self.config)
                if reason is not None:
                    return {"state": "blocked", "reason": reason}
            else:
                label = f"Investigar {technology.name_es} del nivel {upgrade.target_level}" if upgrade.kind == "research" else f"Expandir {technology.name_es} al nivel {upgrade.target_level}"
                button = card.get_by_role("button", name=label, exact=True)
            if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
                return {"state": "blocked", "reason": "upgrade_button_unavailable"}
            if self.session.defense_paused is True:
                return {"state": "blocked", "reason": "defender_paused_routine_actions"}
            identifier = self.journal.prepare(upgrade)
            try:
                await button.click(timeout=self.config.action_timeout_seconds * 1000)
                self.journal.mark(identifier, "submitted")
            except BaseException:
                self.journal.mark(identifier, "uncertain")
                raise
            # La aceptación de un clic no acredita el nivel. La próxima lectura reconcilia.
            return {"state": "pending", "order_id": identifier, "reason": "awaiting_target_level"}
