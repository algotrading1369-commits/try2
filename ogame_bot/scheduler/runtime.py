"""Conecta los seis módulos con un único scheduler y sus dependencias."""
from __future__ import annotations

from typing import Any

from ..adapters.ogame.reader import OGameReader
from ..brain.service import Brain
from ..config import Config
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


class BotRuntime:
    def __init__(self, config: Config, session: SessionManager, journal: OrderJournal | None, defender: Defender | None, *, mode: str = "full") -> None:
        if mode not in {"full", "session", "scanner", "defender"}:
            raise ValueError("Modo continuo desconocido.")
        self.config, self.session, self.journal, self.defender = config, session, journal, defender
        self.latest: AccountSnapshot | None = None
        self._snapshot_finished = float("-inf")
        self.scheduler = Scheduler(config.scheduler, watchdog=session._check_open, on_recoverable=self._recoverable)
        self.scanner = Scanner(OGameReader(session), config)
        self.upgrades = ActionExecutor(session, config, journal) if journal is not None else None
        self.fleet_executor = FleetExecutor(session, config, journal) if journal is not None else None
        self.brain = Brain(config, self.upgrades, journal) if journal is not None else None
        self.fleet = FleetManager(config, FleetReader(session, config), self.fleet_executor, journal) if journal is not None else None
        scheduler = self.scheduler
        scheduler.add("session", self._session, lambda: (config.health_check_seconds, config.health_check_seconds), immediate=False)
        if defender is not None and mode in {"full", "defender"}:
            session.defense_paused = True
            scheduler.add("defender", self._defend, self._defender_interval, critical=True)
        if mode in {"full", "scanner"} and (config.scanner_enabled or mode == "scanner"):
            scheduler.add("scanner", self._scan, lambda: (config.scanner_min_interval_seconds, config.scanner_max_interval_seconds), allowed=self._session_ready)
        if journal is not None and mode in {"full", "defender"}:
            scheduler.add("reconcile", self._reconcile, lambda: (config.scheduler.reconciliation_min_seconds, config.scheduler.reconciliation_max_seconds), allowed=self._reconciliation_ready)
        if mode == "full":
            if self.brain is not None and (config.brain.builder_enabled or config.brain.research_enabled or config.brain.probe_target):
                scheduler.add("brain", self._brain, lambda: (config.brain.min_interval_seconds, config.brain.max_interval_seconds), allowed=self._routine_ready)
            if self.fleet is not None and config.fleet.enabled:
                scheduler.add("fleet", self._fleets, lambda: (config.fleet.min_interval_seconds, config.fleet.max_interval_seconds), allowed=self._routine_ready)

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
            if self.defender is not None:
                self.session.defense_paused = True
            if name != "session":
                self._request_recovery()

    async def _session(self) -> None:
        previous_state = self.session.state
        previous_generation = self.session.authentication_generation
        await self.session.check()
        if self.defender is not None and (previous_state != SessionState.READY or previous_generation != self.session.authentication_generation):
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
