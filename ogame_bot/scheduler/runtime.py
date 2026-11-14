"""Conecta los seis módulos con un único scheduler y sus dependencias."""
from __future__ import annotations

from typing import Any
import logging

from ..adapters.ogame.reader import OGameReader
from ..brain.service import Brain
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
        self.scheduler = Scheduler(config.scheduler, watchdog=self._watchdog, on_recoverable=self._recoverable, on_idle=self._reload_instructions)
        self.scanner = Scanner(OGameReader(session), config)
        self.upgrades = ActionExecutor(session, config, journal) if journal is not None else None
        self.fleet_executor = FleetExecutor(session, config, journal) if journal is not None else None
        self.brain = Brain(config, self.upgrades, journal) if journal is not None else None
        self.fleet = FleetManager(config, FleetReader(session, config), self.fleet_executor, journal) if journal is not None else None
        self._configure_jobs()
        report_config(config, self._applied_version, "applied", mode=mode)

    def _configure_jobs(self) -> None:
        config, mode, session, journal, defender = self.config, self.mode, self.session, self.journal, self.defender
        scheduler = self.scheduler
        previous = scheduler.jobs.copy()
        scheduler.jobs.clear()
        scheduler.add("session", self._session, lambda: (config.health_check_seconds, config.health_check_seconds), immediate=False)
        if defender is not None and config.defender.enabled and mode in {"full", "defender"}:
            session.defense_paused = True
            scheduler.add("defender", self._defend, self._defender_interval, critical=True)
        else:
            session.defense_paused = False
        if mode in {"full", "scanner"} and (config.scanner_enabled or mode == "scanner"):
            scheduler.add("scanner", self._scan, lambda: (config.scanner_min_interval_seconds, config.scanner_max_interval_seconds), allowed=self._session_ready)
        if journal is not None and mode in {"full", "defender"}:
            scheduler.add("reconcile", self._reconcile, lambda: (config.scheduler.reconciliation_min_seconds, config.scheduler.reconciliation_max_seconds), allowed=self._reconciliation_ready)
        if mode == "full":
            if self.brain is not None and (config.brain.builder_enabled or config.brain.research_enabled or config.brain.probe_target):
                scheduler.add("brain", self._brain, lambda: (config.brain.min_interval_seconds, config.brain.max_interval_seconds), allowed=self._routine_ready)
            if self.fleet is not None and config.fleet.enabled:
                scheduler.add("fleet", self._fleets, lambda: (config.fleet.min_interval_seconds, config.fleet.max_interval_seconds), allowed=self._routine_ready)
        for name, job in scheduler.jobs.items():
            if name in previous:
                for key in ("runs", "failures", "consecutive_failures", "deferred", "last_result", "last_error_type", "last_started_at", "last_finished_at", "last_duration_seconds"):
                    setattr(job, key, getattr(previous[name], key))

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
                alerts = self.defender.alerts
                alerts.settings, alerts.sender = candidate.telegram, sender
                self.defender = Defender(self.session, candidate, self.journal, alerts)
            self.latest = None
            self._configure_jobs()
            self._applied_version = version
            report_config(candidate, version, "applied", mode=self.mode)
            logger.info("Instrucciones del panel aplicadas sin cerrar la sesión del juego.")
        except (CoreError, OSError, ValueError):
            report_config(self.config, version, "rejected", mode=self.mode, error="No se pudieron aplicar las instrucciones. Se conserva la configuración anterior.")
            logger.warning("No se pudieron aplicar las instrucciones actualizadas.")

    def _session_ready(self) -> str | None:
        return None if self.session.state == SessionState.READY else "session_not_ready"

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
        self.latest = None
        self.latest = await self.scanner.scan()
        self._snapshot_finished = self.scheduler.clock()

    async def _fresh_snapshot(self) -> AccountSnapshot:
        if self.latest is None or self.scheduler.clock() - self._snapshot_finished > self.config.scanner_max_reading_age_seconds:
            await self.scheduler.run_now("scanner")
        if self.latest is None:
            raise DependencyPending()
        return self.latest

    async def _brain(self) -> None:
        if self.brain is not None:
            await self.brain.process(await self._fresh_snapshot())

    async def _fleets(self) -> None:
        if self.fleet is not None:
            await self.fleet.process(await self._fresh_snapshot())

    async def _reconcile(self) -> None:
        if self.upgrades is not None and self.journal is not None and self.journal.pending("upgrade") is not None:
            await self.upgrades.reconcile()
        if self.fleet_executor is not None and self.journal is not None and self.journal.pending("fleet") is not None:
            await self.fleet_executor.reconcile()

    async def run(self) -> None:
        previous = self.session.before_navigation
        self.session.before_navigation = self.scheduler.checkpoint
        try:
            await self.scheduler.run()
        finally:
            self.session.before_navigation = previous
