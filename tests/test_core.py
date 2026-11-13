"""Pruebas de configuración y sesión contra un servidor totalmente simulado."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import Error, Route

from ogame_bot.config import load_config
from ogame_bot.core.browser import BrowserManager
from ogame_bot.core.locking import profile_lock
from ogame_bot.core.session import SessionManager, SessionState
from ogame_bot.core.timing import backoff
from ogame_bot.errors import ConfigurationError, ConnectionLost, CoreError, IdentityMismatch, LoginRequired, ReadError

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_paths_are_relative_to_config(self) -> None:
        config = load_config(ROOT / "config.example.yaml")
        self.assertEqual(config.profile_dir, ROOT / "runtime" / "browser-profile")
        self.assertEqual(config.server_origin, "https://s277-es.ogame.gameforge.com")

    def test_reject_invalid_configuration(self) -> None:
        original = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        for old, new in [
            ("max_delay_seconds: 4.0", "max_delay_seconds: 0"),
            ("server_number: 277", "server_number: true"),
            ("health_check_seconds: 30", "health_check_seconds: .nan"),
            ("community: es", "community: 'es.evil.com'"),
            ("headless: false", "headless: 'false'"),
            ("rotation_mb: 5", "rotaton_mb: 5"),
        ]:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(original.replace(old, new), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_config(path)

    def test_profile_lock_excludes_second_controller_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            with profile_lock(profile):
                with self.assertRaises(CoreError):
                    with profile_lock(profile):
                        self.fail("Se adquirió dos veces el mismo perfil")
            with profile_lock(profile):
                self.assertTrue(profile.parent.exists())

    def test_backoff_remains_bounded(self) -> None:
        for attempt in (0, 1, 5, 1000):
            delay = backoff(attempt, 5, 120)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 120)


class SessionLogicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = replace(
            load_config(ROOT / "config.example.yaml"), headless=True,
            min_delay_seconds=0, max_delay_seconds=0,
            max_reconnect_attempts=2,
            initial_backoff_seconds=0.001, max_backoff_seconds=0.001,
        )
        self.locator = MagicMock()
        self.locator.count = AsyncMock(return_value=1)
        self.locator.is_visible = AsyncMock(return_value=True)
        self.locator.inner_text = AsyncMock(return_value="Test Commander")
        self.locator.wait_for = AsyncMock()
        self.page = MagicMock()
        self.page.url = self.config.overview_url
        self.page.is_closed.return_value = False
        self.page.locator.return_value = self.locator
        self.page.goto = AsyncMock(return_value=MagicMock(status=200))
        self.context = MagicMock()
        self.context.pages = [self.page]
        self.context.new_page = AsyncMock(return_value=self.page)
        self.session = SessionManager(self.context, self.config)

    async def test_connect_refreshes_existing_dom(self) -> None:
        await self.session.connect()
        self.assertEqual(self.session.state, SessionState.READY)
        self.page.goto.assert_awaited_once()

    async def test_identity_mismatch_stops_without_navigation(self) -> None:
        self.locator.inner_text.return_value = "Otra cuenta"
        with self.assertRaises(IdentityMismatch):
            await self.session.connect()
        self.page.goto.assert_not_awaited()

    async def test_session_expiration_is_not_ready(self) -> None:
        await self.session.connect()
        self.locator.count.return_value = 0
        with self.assertRaises(LoginRequired):
            await self.session.check()
        self.assertNotEqual(self.session.state, SessionState.READY)

    async def test_network_reconnect_is_bounded(self) -> None:
        self.page.goto.side_effect = Error("Synthetic network failure")
        with self.assertRaises(ConnectionLost):
            await self.session.connect()
        self.assertEqual(self.page.goto.await_count, 2)

    async def test_network_recovers(self) -> None:
        self.page.goto.side_effect = [Error("Synthetic failure"), MagicMock(status=200)]
        await self.session.connect()
        self.assertEqual(self.session.state, SessionState.READY)
        self.assertEqual(self.page.goto.await_count, 2)

    async def test_operations_are_serialized(self) -> None:
        active = 0
        peak = 0

        async def navigate(*args: object, **kwargs: object) -> MagicMock:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return MagicMock(status=200)

        self.page.goto.side_effect = navigate
        await asyncio.gather(self.session.check(), self.session.check())
        self.assertEqual(peak, 1)

    async def test_cancellation_releases_lock_and_stops(self) -> None:
        started = asyncio.Event()

        async def navigate(*args: object, **kwargs: object) -> None:
            started.set()
            await asyncio.Event().wait()

        self.page.goto.side_effect = navigate
        task = asyncio.create_task(self.session.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.session.action_lock.locked())
        self.assertEqual(self.session.state, SessionState.STOPPED)

    async def test_server_hostname_must_match_exactly(self) -> None:
        self.assertTrue(self.session.is_game_url(self.config.overview_url))
        self.assertFalse(self.session.is_game_url("https://s277-es.ogame.gameforge.com.evil.com/game/index.php"))
        self.assertFalse(self.session.is_game_url("https://s278-es.ogame.gameforge.com/game/index.php"))

    async def test_read_navigation_is_locked_and_has_only_expected_query(self) -> None:
        await self.session.connect()

        async def navigate(url: str, **kwargs: object) -> MagicMock:
            self.page.url = url
            return MagicMock(status=200)

        self.page.goto.side_effect = navigate
        async with self.session.read_page("supplies", 101) as page:
            self.assertIs(page, self.page)
            self.assertTrue(self.session.action_lock.locked())
            self.assertEqual(page.url, self.config.server_origin + "/game/index.php?page=ingame&component=supplies&cp=101")
        self.assertFalse(self.session.action_lock.locked())

    async def test_scheduler_checkpoint_runs_before_acquiring_browser_lock(self) -> None:
        await self.session.connect()
        async def checkpoint():
            self.assertFalse(self.session.action_lock.locked())
        self.session.before_navigation = AsyncMock(side_effect=checkpoint)
        async with self.session.read_page("overview"):
            self.assertTrue(self.session.action_lock.locked())
        self.session.before_navigation.assert_awaited_once()

    async def test_read_redirect_to_lobby_requires_reauthentication(self) -> None:
        await self.session.connect()

        async def navigate(*args: object, **kwargs: object) -> MagicMock:
            self.page.url = self.config.lobby_url
            return MagicMock(status=200)

        self.page.goto.side_effect = navigate
        with self.assertRaises(LoginRequired):
            async with self.session.read_page("overview"):
                self.fail("La lectura no debe continuar sin autenticación")
        self.assertEqual(self.session.state, SessionState.WAITING_LOGIN)
        self.assertFalse(self.session.action_lock.locked())

    async def test_wrong_component_is_not_accepted_as_requested_section(self) -> None:
        await self.session.connect()
        with self.assertRaises(ReadError):
            async with self.session.read_page("supplies", 101):
                self.fail("El componente overview no debe aceptarse como supplies")


@unittest.skipUnless(os.environ.get("OGAME_BROWSER_TESTS") == "1", "Pruebas de navegador optativas: OGAME_BROWSER_TESTS=1")
class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(
            load_config(ROOT / "config.example.yaml"), headless=True,
            profile_dir=Path(self.temp.name) / "profile",
            min_delay_seconds=0, max_delay_seconds=0,
            action_timeout_seconds=0.1,
            max_reconnect_attempts=2,
            initial_backoff_seconds=0.001, max_backoff_seconds=0.001,
        )
        self.browser = BrowserManager(self.config)
        await self.browser.__aenter__()
        self.context = self.browser.require_context()
        self.player = "Test Commander"
        self.expired = False
        self.unavailable = False
        self.requests = 0
        await self.context.route("**/*", self.respond)

    async def asyncTearDown(self) -> None:
        await self.browser.__aexit__(None, None, None)
        self.temp.cleanup()

    async def respond(self, route: Route) -> None:
        self.requests += 1
        if self.unavailable:
            await route.fulfill(status=503, body="Servicio no disponible")
        elif self.expired:
            await route.fulfill(status=200, body="<html>Iniciar sesión</html>")
        else:
            await route.fulfill(status=200, content_type="text/html", body=f'<span id="playerName">{self.player}</span>')

    async def test_connection_and_server_validation(self) -> None:
        session = SessionManager(self.context, self.config)
        await session.connect()
        self.assertEqual(session.state, SessionState.READY)
        self.assertFalse(session.is_game_url("https://s277-es.ogame.gameforge.com.evil.com/game/index.php"))
        self.assertFalse(session.is_game_url("https://s278-es.ogame.gameforge.com/game/index.php"))

    async def test_wrong_identity_stops(self) -> None:
        self.player = "Otra cuenta"
        with self.assertRaises(IdentityMismatch):
            await SessionManager(self.context, self.config).connect()

    async def test_check_detects_expiration_despite_old_dom(self) -> None:
        session = SessionManager(self.context, self.config)
        await session.connect()
        self.expired = True
        with self.assertRaises(LoginRequired):
            await session.check()
        self.assertNotEqual(session.state, SessionState.READY)

    async def test_transient_http_failure_retries(self) -> None:
        session = SessionManager(self.context, self.config)
        original = self.respond
        calls = 0

        async def temporary_failure(route: Route) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                await route.fulfill(status=503, body="Temporal")
            else:
                await original(route)

        await self.context.unroute("**/*")
        await self.context.route("**/*", temporary_failure)
        await session.connect()
        self.assertEqual(session.state, SessionState.READY)
        self.assertEqual(calls, 2)

    async def test_persistent_failure_is_bounded(self) -> None:
        self.unavailable = True
        with self.assertRaises(ConnectionLost):
            await SessionManager(self.context, self.config).connect()
        self.assertEqual(self.requests, 2)

    async def test_lobby_popup_is_detected(self) -> None:
        config = replace(self.config, headless=False, manual_login_timeout_seconds=2)
        session = SessionManager(self.context, config)
        popup = await self.context.new_page()
        await popup.goto(config.overview_url)
        await session._wait_for_manual_login()
        self.assertIs(session.page, popup)
        self.assertEqual(session.state, SessionState.READY)

    async def test_profile_cookies_survive_restart(self) -> None:
        await self.context.add_cookies([{
            "name": "synthetic-session", "value": "test-only",
            "url": self.config.server_origin, "expires": 2000000000,
        }])
        await self.browser.__aexit__(None, None, None)
        self.browser = BrowserManager(self.config)
        await self.browser.__aenter__()
        cookies = await self.browser.require_context().cookies()
        self.assertTrue(any(cookie["name"] == "synthetic-session" for cookie in cookies))


if __name__ == "__main__":
    unittest.main()
