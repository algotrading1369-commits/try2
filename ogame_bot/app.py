"""Entradas de una ejecución y modos continuos coordinados por el scheduler."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .adapters.ogame.reader import OGameReader
from .brain.service import Brain
from .config import Config
from .core.action_executor import ActionExecutor
from .core.browser import BrowserManager
from .core.session import SessionManager, SessionState
from .defender.service import Defender
from .fleet.executor import FleetExecutor
from .fleet.reader import FleetReader
from .fleet.service import FleetManager
from .observability.telegram import AlertDispatcher
from .scanner.service import Scanner
from .scheduler.runtime import BotRuntime
from .storage.orders import OrderJournal
from .core.panel_control import panel_stop_requested

logger = logging.getLogger(__name__)


async def run(config: Config, *, scan_once: bool = False, session_only: bool = False, scanner_only: bool = False, brain_once: bool = False, fleet_once: bool = False, defender_once: bool = False, defender_only: bool = False) -> None:
    session: SessionManager | None = None

    def observe(value: SessionManager) -> None:
        nonlocal session
        session = value

    task = asyncio.create_task(_run(config, scan_once=scan_once, session_only=session_only, scanner_only=scanner_only, brain_once=brain_once, fleet_once=fleet_once, defender_once=defender_once, defender_only=defender_only, observer=observe))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.2)
            if panel_stop_requested(config.scheduler.status_file.with_name("panel_stop.json")):
                if session is None or not session.transaction_active:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    logger.info("Bot detenido desde el panel; proceso y navegador liberados.")
                    return
        await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _run(config: Config, *, scan_once: bool = False, session_only: bool = False, scanner_only: bool = False, brain_once: bool = False, fleet_once: bool = False, defender_once: bool = False, defender_only: bool = False, observer: Callable[[SessionManager], None] | None = None) -> None:
    flags = (scan_once, session_only, scanner_only, brain_once, fleet_once, defender_once, defender_only)
    if sum(bool(flag) for flag in flags) > 1:
        raise ValueError("Los modos de ejecución son excluyentes.")
    async with BrowserManager(config) as browser:
        session = SessionManager(browser.require_context(), config)
        if observer is not None:
            observer(session)
        journal: OrderJournal | None = None
        alerts: AlertDispatcher | None = None
        try:
            # No vincular una base de órdenes a un nick de ejemplo antes del acceso real.
            await session.connect()
            if not any((session_only, scan_once, scanner_only)):
                journal = OrderJournal.open_for_account(config.brain.journal_file, config.server_origin + "|" + config.expected_player_name)
            full_mode = not any(flags)
            defender = None
            if journal is not None and (defender_once or defender_only or full_mode):
                alerts = AlertDispatcher(config.telegram)
                defender = Defender(session, config, journal, alerts)
            if defender_once or (defender_only and not config.defender.enabled):
                if defender is not None:
                    await defender.process()
                return
            if scan_once or brain_once or fleet_once:
                snapshot = await Scanner(OGameReader(session), config).scan()
                if journal is not None and brain_once:
                    await Brain(config, ActionExecutor(session, config, journal), journal).process(snapshot)
                if journal is not None and fleet_once:
                    await FleetManager(config, FleetReader(session, config), FleetExecutor(session, config, journal), journal).process(snapshot)
                return
            mode = "session" if session_only else "scanner" if scanner_only else "defender" if defender_only else "full"
            await BotRuntime(config, session, journal, defender, mode=mode).run()
        finally:
            session.state = SessionState.STOPPED
            try:
                if alerts is not None:
                    await alerts.close()
            finally:
                if journal is not None:
                    journal.close()
