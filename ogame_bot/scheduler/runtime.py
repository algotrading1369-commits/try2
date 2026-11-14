"""Conecta los seis módulos con un único scheduler y sus dependencias."""
from __future__ import annotations

from typing import Any
import logging

from ..adapters.ogame.reader import OGameReader
from ..brain.service import Brain
from ..brain.planner import Upgrade
from ..config import Config, load_config
from ..live_config import config_version, validate_live_change, report_config
from ..errors import CoreError
from ..observability.telegram import TelegramTransport
from ..core.action_executor import ActionExecutor
from ..core.session import SessionManager, SessionState
from ..defender.service import Defender
from ..domain.models import AccountSnapshot
from ..errors import ConnectionLost, LoginRequired
from ..fleet.executor import FleetExecutor
from ..fleet.reader import FleetReader
from ..fleet.service import FleetManager
from ..scanner.service import Scanner
from ..storage.orders import OrderJournal
from .loop import DependencyPending, Scheduler
from .waiting import next_economic_check, seconds_until
from ..storage.snapshots import save_snapshot

logger = logging.getLogger(__name__)


class BotRuntime:
    def __init__(self, config: Config, session: SessionManager, journal: OrderJournal | None, defender: Defender | None, *, mode: str = "full") -> None:
        if mode not in {"full", "session", "scanner", "defender"}:
            raise ValueError("Modo continuo desconocido.")
        self.config, self.session, self.journal, self.defender = config, session, journal, defender
        self.mode = mode
        self._seen_version = config.source_version
        self._applied_version = self._seen_version
        self.latest: AccountSnapshot | None = None
        self._snapshot_finished = float("-inf")
        self._read_requested = True
        self._full_scan_requested = False
        self._order_due: dict[str, float] = {}
        self.scheduler = Scheduler(config.scheduler, watchdog=self._watchdog, on_recoverable=self._recoverable, on_idle=self._reload_instructions)
        self.scanner = Scanner(OGameReader(session), config)
        self.upgrades = ActionExecutor(session, config, journal) if journal is not None else None
        self.fleet_executor = FleetExecutor(session, config, journal) if journal is not None else None
        self.brain = Brain(config, self.upgrades, journal) if journal is not None else None
        self.fleet = FleetManager(config, FleetReader(session, config), self.fleet_executor, journal) if journal is not None else None
        self._configure_jobs()
        report_config(config, self._applied_version, "applied", mode=mode)

    def _configure_jobs(self, reset: set[str] | None = None) -> None:
        config, mode, session, journal, defender = self.config, self.mode, self.session, self.journal, self.defender
        scheduler = self.scheduler
        previous = scheduler.jobs.copy()
        scheduler.jobs.clear()
        scheduler.add("session", self._session, lambda: (config.health_check_seconds, config.health_check_seconds), immediate=False)
        if defender is not None and config.defender.enabled and mode in {"full", "defender"}:
            if "defender" not in previous:
                session.defense_paused = True
            scheduler.add("defender", self._defend, self._defender_interval, critical=True)
        else:
            session.defense_paused = False
        if mode in {"full", "scanner"} and (config.scanner_enabled or mode == "scanner"):
            scheduler.add("scanner", self._scan, lambda: (config.scanner_min_interval_seconds, config.scanner_max_interval_seconds), allowed=self._scanner_ready)
        if journal is not None and mode in {"full", "defender"}:
            scheduler.add("reconcile", self._reconcile, lambda: (config.scheduler.reconciliation_min_seconds, config.scheduler.reconciliation_max_seconds), allowed=self._reconciliation_ready)
        if mode == "full":
            if self.brain is not None and (config.brain.builder_enabled or config.brain.research_enabled or config.brain.probe_target or config.brain.shipyard_enabled):
                scheduler.add("brain", self._brain, lambda: (config.brain.min_interval_seconds, config.brain.max_interval_seconds), allowed=self._brain_ready)
            if self.fleet is not None and config.fleet.enabled:
                scheduler.add("fleet", self._fleets, lambda: (config.fleet.min_interval_seconds, config.fleet.max_interval_seconds), allowed=self._routine_ready)
        for name, job in scheduler.jobs.items():
            if name in previous:
                for key in ("runs", "failures", "consecutive_failures", "deferred", "last_result", "last_error_type", "last_started_at", "last_finished_at", "last_duration_seconds"):
                    setattr(job, key, getattr(previous[name], key))
                if reset is not None and name not in reset:
                    job.next_due = previous[name].next_due
                    job.waiting_reason = previous[name].waiting_reason
                elif name in {"session", "defender"}:
                    job.next_due = scheduler.clock() + scheduler._delay(job)

    def _watchdog(self) -> None:
        self.session._check_open()
        self._reload_instructions()

    def _reload_instructions(self) -> None:
        if self.scheduler.stack or self.config.source_file is None:
            return
        version: str | None = None
        try:
            version = config_version(self.config)
            if version == self._seen_version:
                return
            self._seen_version = version
            candidate = load_config(self.config.source_file)
            if candidate.source_version != version:
                self._seen_version = None
                return
            validate_live_change(self.config, candidate)
            sender = TelegramTransport(candidate.telegram).send if self.defender is not None else None
            previous = self.config
            reset: set[str] = set()
            if previous.health_check_seconds != candidate.health_check_seconds:
                reset.add("session")
            if previous.defender != candidate.defender:
                reset.add("defender")
            if previous.brain != candidate.brain:
                reset.update({"brain", "scanner"})
                self._read_requested = True
            if previous.fleet != candidate.fleet:
                reset.add("fleet")
            self.config = candidate
            self.session.config = candidate
            self.session.context.set_default_timeout(candidate.action_timeout_seconds * 1000)
            self.session.context.set_default_navigation_timeout(candidate.navigation_timeout_seconds * 1000)
            self.scanner = Scanner(OGameReader(self.session), candidate)
            self.upgrades = ActionExecutor(self.session, candidate, self.journal) if self.journal is not None else None
            self.fleet_executor = FleetExecutor(self.session, candidate, self.journal) if self.journal is not None else None
            self.brain = Brain(candidate, self.upgrades, self.journal) if self.journal is not None else None
            self.fleet = FleetManager(candidate, FleetReader(self.session, candidate), self.fleet_executor, self.journal) if self.journal is not None else None
            if self.defender is not None:
                last_state = self.defender.last_state
                alerts = self.defender.alerts
                alerts.settings, alerts.sender = candidate.telegram, sender
                self.defender = Defender(self.session, candidate, self.journal, alerts)
                self.defender.last_state = last_state
            self._configure_jobs(reset)
            self._applied_version = version
            report_config(candidate, version, "applied", mode=self.mode)
            logger.info("Instrucciones del panel aplicadas sin cerrar la sesión del juego.")
        except (CoreError, OSError, ValueError):
            report_config(self.config, version, "rejected", mode=self.mode, error="No se pudieron aplicar las instrucciones. Se conserva la configuración anterior.")
            logger.warning("No se pudieron aplicar las instrucciones actualizadas.")

    def _session_ready(self) -> str | None:
        return None if self.session.state == SessionState.READY else "session_not_ready"

    def _scanner_ready(self) -> str | None:
        if reason := self._session_ready():
            return reason
        return "waiting_for_event" if self.mode == "full" and self.latest is not None and not self._read_requested else None

    def _brain_ready(self) -> str | None:
        if reason := self._routine_ready():
            return reason
        return None

    def _routine_ready(self) -> str | None:
        if reason := self._session_ready():
            return reason
        if self.session.defense_paused:
            return "defender_paused"
        return "snapshot_not_ready" if self.latest is None else None

    def _reconciliation_ready(self) -> str | None:
        if reason := self._session_ready():
            return reason
        return None if self.journal is not None and self.journal.pending() is not None else "no_pending_orders"

    def _request_recovery(self) -> None:
        # Un aviso de otro módulo no borra el backoff de sesión ya activo.
        if self.scheduler.jobs["session"].last_result != "retrying":
            self.scheduler.trigger("session")

    def _recoverable(self, name: str, error: Exception) -> None:
        if isinstance(error, (LoginRequired, ConnectionLost)):
            self.session.state = SessionState.WAITING_LOGIN
            if self.defender is not None and self.config.defender.enabled:
                self.session.defense_paused = True
            if name != "session":
                self._request_recovery()

    async def _session(self) -> None:
        previous_state = self.session.state
        previous_generation = self.session.authentication_generation
        if self.session.state == SessionState.READY:
            await self.session.observe()
        else:
            await self.session.check()
        if self.defender is not None and self.config.defender.enabled and (previous_state != SessionState.READY or previous_generation != self.session.authentication_generation):
            self.session.defense_paused = True
            self.scheduler.trigger("defender")

    def _defender_interval(self) -> tuple[float, float]:
        settings = self.config.defender
        return (settings.idle_min_seconds, settings.idle_max_seconds) if self.defender is not None and self.defender.last_state == "clear" else (settings.alert_min_seconds, settings.alert_max_seconds)

    async def _defend(self) -> None:
        if self.defender is not None:
            await self.defender.process()
            if self.session.state != SessionState.READY:
                self._request_recovery()

    async def _scan(self) -> None:
        self.scanner.shipyard_required = self.config.brain.shipyard_enabled or bool(self.journal and any(action.kind == "shipyard" for _,action in self.journal.pending_upgrades()))
        if self.latest is None or self._full_scan_requested or self.mode != "full" or self.config.brain.probe_target:
            self.latest = await self.scanner.scan()
        else:
            self.latest = await self.scanner.refresh_economy(self.latest)
        self._read_requested = False
        self._full_scan_requested = False
        self._snapshot_finished = self.scheduler.clock()
        self._confirm_snapshot_upgrade()
        if self.journal is not None:
            for identifier, action in self.journal.pending_upgrades():
                queue = self.latest.research_queue if action.kind == "research" else next((body.shipyard_queue if action.kind == "shipyard" else body.construction_queue for body in self.latest.bodies if body.body.id == action.body_id), None)
                delay = seconds_until((queue.data or {}).get("ends_at")) if queue else None
                if delay is not None or self._order_due.get(identifier, 0) <= self.scheduler.clock():
                    self._order_due[identifier] = self.scheduler.clock() + max(30.0 if action.kind == "shipyard" else 5.0, delay or self.config.brain.idle_retry_seconds)
            self._schedule_reconciliation()

    def _confirm_snapshot_upgrade(self) -> None:
        if self.latest is None or self.journal is None:
            return
        for identifier, action in self.journal.pending_upgrades():
            self._confirm_snapshot_order(identifier, action)

    def _confirm_snapshot_order(self, identifier: str, action: Upgrade) -> None:
        assert self.latest is not None and self.journal is not None
        snapshot = self.latest
        if snapshot.player != self.config.expected_player_name or snapshot.server != self.config.server_origin:
            return
        body = next((body for body in snapshot.bodies if body.body.id == action.body_id and body.body.coordinates == action.coordinates), None)
        if body is None:
            return
        reading = snapshot.research if action.kind == "research" else body.ships if action.kind == "shipyard" else body.facilities if action.technology.component == "facilities" else body.buildings
        value = (reading.data or {}).get(action.item)
        production_finished = action.kind != "shipyard" or (body.shipyard_queue.is_fresh(self.config.scanner_max_reading_age_seconds) and (body.shipyard_queue.data or {}).get("state") == "free")
        if reading.is_fresh(self.config.scanner_max_reading_age_seconds) and type(value) is int and value >= action.target_level and production_finished:
            self.journal.mark(identifier, "confirmed")
            self._order_due.pop(identifier, None)
            logger.info("Orden confirmada en la lectura existente: %s, nivel %s.", action.item, action.target_level)
            self.scheduler.trigger("brain")

    async def _fresh_snapshot(self) -> AccountSnapshot:
        if self.latest is None or self._read_requested or self.scheduler.clock() - self._snapshot_finished > self.config.scanner_max_reading_age_seconds:
            self._read_requested = True
            await self.scheduler.run_now("scanner")
        if self.latest is None:
            raise DependencyPending()
        return self.latest

    async def _brain(self) -> None:
        if self.brain is not None:
            snapshot = await self._fresh_snapshot()
            report = await self.brain.process(snapshot)
            delay, reason = next_economic_check(self.config, snapshot, report)
            result = report.get("result", {})
            if (report.get("selected") or {}).get("kind") == "shipyard":
                delay = max(30.0, delay)
            if result.get("state") == "pending" and result.get("order_id"):
                self._order_due[result["order_id"]] = self.scheduler.clock() + delay
            report["next_check_seconds"], report["waiting_reason"] = delay, reason
            save_snapshot(self.config.brain.plan_file, report)
            self.scheduler.defer("brain", delay, reason)
            self._schedule_reconciliation()
            if result.get("state") == "pending":
                # Refresh the spent resources and evaluate the other game queue.
                # The journal filters this queue even if its DOM still says free.
                self._read_requested = True
                self.scheduler.trigger("scanner")
                self.scheduler.trigger("brain")
            logger.info("Espera económica: %.0f segundos; motivo: %s.", delay, reason)

    async def _fleets(self) -> None:
        if self.fleet is not None:
            if self.latest is None or (self.config.fleet.transports["enabled"] and not self.latest.registry.is_fresh(self.config.scanner_max_reading_age_seconds)):
                self._read_requested = self._full_scan_requested = True
                await self.scheduler.run_now("scanner")
            if self.latest is None:
                raise DependencyPending()
            report = await self.fleet.process(self.latest)
            if report.get("result", {}).get("state") == "confirmed":
                # Re-read actual slots and ships before filling the next slot.
                self.scheduler.trigger("fleet")
                self._read_requested = True
                self.scheduler.trigger("scanner")

    async def _reconcile(self) -> None:
        if self.upgrades is not None and self.journal is not None:
            # A due completion causes one economy read, which confirms ALL
            # completed queues and refreshes resources before choosing again.
            due = [order for order in self.journal.pending_upgrades() if self._order_due.get(order[0], 0) <= self.scheduler.clock()]
            if due:
                if "scanner" in self.scheduler.jobs:
                    self._read_requested = True
                    self.scheduler.trigger("scanner")
                    self.scheduler.trigger("brain")
                    for identifier, _ in due:
                        self._order_due[identifier] = self.scheduler.clock() + self.config.brain.idle_retry_seconds
                else:
                    for identifier, action in due:
                        result = await self.upgrades.reconcile((identifier, action))
                        self._order_due[identifier] = self.scheduler.clock() + max(30.0, seconds_until(result.get("retry_at")) or self.config.brain.idle_retry_seconds)
            self._schedule_reconciliation()
        if self.fleet_executor is not None and self.journal is not None and self.journal.pending("fleet") is not None:
            result = await self.fleet_executor.reconcile()
            if result.get("state") == "confirmed":
                self.scheduler.trigger("fleet")

    def _schedule_reconciliation(self) -> None:
        if self.journal is None:
            return
        orders = self.journal.pending_upgrades()
        if orders and "reconcile" in self.scheduler.jobs:
            delay = min(max(0.01, self._order_due.get(identifier, self.scheduler.clock()) - self.scheduler.clock()) for identifier, _ in orders)
            self.scheduler.defer("reconcile", delay, "waiting_for_completion")

    async def run(self) -> None:
        previous = self.session.before_navigation
        self.session.before_navigation = self.scheduler.checkpoint
        try:
            await self.scheduler.run()
        finally:
            self.session.before_navigation = previous
