"""Bounded Lobby reentry using the observed ES account-row contract."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeout
from ogame_bot.config import load_config
from ogame_bot.core.session import SessionManager, SessionState
from ogame_bot.errors import IdentityMismatch, LoginRequired


class LobbyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = replace(load_config(Path(__file__).resolve().parents[1] / 'config.example.yaml'),
            headless=True, min_delay_seconds=0, max_delay_seconds=0)
        self.page = MagicMock()
        self.page.url = 'https://lobby.ogame.gameforge.com/es_ES/accounts'
        self.page.is_closed.return_value = False
        self.rows = MagicMock()
        self.rows.filter.return_value = self.rows
        self.rows.first.wait_for = AsyncMock()
        self.rows.count = AsyncMock(return_value=1)
        self.button = MagicMock()
        self.button.count = AsyncMock(return_value=1)
        self.button.is_visible = AsyncMock(return_value=True)
        self.button.is_enabled = AsyncMock(return_value=True)
        self.button.click = AsyncMock()
        self.rows.get_by_role.return_value = self.button
        self.link = MagicMock()
        self.link.count = AsyncMock(return_value=1)
        self.link.is_visible = AsyncMock(return_value=True)
        self.link.click = AsyncMock()
        self.link.wait_for = AsyncMock()
        self.page.get_by_role.side_effect = lambda role, **kwargs: self.rows if role == 'row' else self.link
        self.game = MagicMock()
        self.game.url = self.config.overview_url
        self.game.is_closed.return_value = False
        self.game.locator.return_value.count = AsyncMock(return_value=1)
        self.game.locator.return_value.is_visible = AsyncMock(return_value=True)
        self.game.locator.return_value.inner_text = AsyncMock(return_value=self.config.expected_player_name)
        self.game.locator.return_value.wait_for = AsyncMock()
        self.game.goto = AsyncMock(return_value=MagicMock(status=200))
        context = MagicMock()
        context.pages = [self.page, self.game]
        self.session = SessionManager(context, self.config)
        self.session.page = self.page

    async def test_exact_unique_row_opens_and_verifies_game_identity(self) -> None:
        self.assertTrue(await self.session._resume_from_lobby())
        self.button.click.assert_awaited_once()
        self.assertIs(self.session.page, self.game)
        self.assertEqual(self.session.state, SessionState.READY)
        self.assertEqual(self.session.authentication_generation, 1)
        self.game.goto.assert_awaited_once_with(self.config.overview_url, wait_until='domcontentloaded')
        pattern = self.page.locator.return_value.filter.call_args.kwargs['has_text']
        self.assertIsNotNone(pattern.fullmatch(self.config.expected_player_name))
        self.assertIsNone(pattern.fullmatch('Other ' + self.config.expected_player_name))
        self.page.locator.assert_any_call('.country-cell .flag-es')

    async def test_multiple_or_missing_accounts_do_not_click_play(self) -> None:
        for count in (0, 2):
            self.rows.count.return_value = count
            self.assertFalse(await self.session._resume_from_lobby())
        self.button.click.assert_not_awaited()

    async def test_disabled_or_duplicate_play_does_not_click(self) -> None:
        self.button.is_enabled.return_value = False
        self.assertFalse(await self.session._resume_from_lobby())
        self.button.is_enabled.return_value = True
        self.button.count.return_value = 2
        self.assertFalse(await self.session._resume_from_lobby())
        self.button.click.assert_not_awaited()

    async def test_hub_uses_visible_play_link_before_account_row(self) -> None:
        self.page.url = 'https://lobby.ogame.gameforge.com/es_ES/hub'
        self.assertTrue(await self.session._resume_from_lobby())
        self.link.click.assert_awaited_once()

    async def test_slow_lobby_mount_is_awaited_before_counting_play_links(self) -> None:
        self.page.url = 'https://lobby.ogame.gameforge.com/'
        self.link.count.return_value = 0

        async def hydrate(**kwargs: object) -> None:
            self.link.count.return_value = 1

        self.link.wait_for.side_effect = hydrate
        self.assertTrue(await self.session._resume_from_lobby())
        self.link.wait_for.assert_awaited_once()
        self.link.click.assert_awaited_once()

    async def test_slow_accounts_table_is_awaited_before_selecting_player(self) -> None:
        self.rows.count.return_value = 0

        async def load_accounts(**kwargs: object) -> None:
            self.rows.count.return_value = 1

        self.rows.first.wait_for.side_effect = load_accounts
        self.assertTrue(await self.session._resume_from_lobby())
        self.button.click.assert_awaited_once()

    async def test_login_page_without_play_requires_manual_access(self) -> None:
        self.page.url = 'https://lobby.ogame.gameforge.com/es_ES/'
        self.link.count.return_value = 0
        self.link.wait_for.side_effect = PlaywrightTimeout('synthetic login page')
        self.assertFalse(await self.session._resume_from_lobby())
        self.assertEqual(self.session.lobby_recovery_reason, 'play_link_not_ready_or_login_required')
        self.link.click.assert_not_awaited()
        self.button.click.assert_not_awaited()

    async def test_untrusted_lobby_hostname_is_rejected(self) -> None:
        self.page.url = 'https://lobby.ogame.gameforge.com.evil.example/es_ES/accounts'
        self.assertFalse(await self.session._resume_from_lobby())
        self.button.click.assert_not_awaited()

    async def test_wrong_player_in_target_universe_stops_recovery(self) -> None:
        self.game.locator.return_value.inner_text.return_value = 'Another commander'
        with self.assertRaises(IdentityMismatch):
            await self.session._resume_from_lobby()
        self.assertNotEqual(self.session.state, SessionState.READY)

    async def test_stale_authenticated_dom_is_not_accepted_after_failed_refresh(self) -> None:
        async def expire(url: str, **kwargs: object) -> MagicMock:
            self.game.locator.return_value.count.return_value = 0
            return MagicMock(status=200)

        self.game.goto.side_effect = expire
        clock = [0.0]

        async def advance(seconds: float) -> None:
            clock[0] += seconds

        with patch('ogame_bot.core.session.time', SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(self.session, '_interruptible_wait', side_effect=advance):
            self.assertFalse(await self.session._resume_from_lobby())
        self.assertNotEqual(self.session.state, SessionState.READY)
        self.assertEqual(self.session.authentication_generation, 0)

    async def test_new_game_tab_is_tried_before_stale_authenticated_tab(self) -> None:
        fresh = MagicMock()
        fresh.url = self.config.overview_url
        fresh.is_closed.return_value = False
        fresh.locator.return_value.count = AsyncMock(return_value=1)
        fresh.locator.return_value.is_visible = AsyncMock(return_value=True)
        fresh.locator.return_value.inner_text = AsyncMock(return_value=self.config.expected_player_name)
        fresh.locator.return_value.wait_for = AsyncMock()
        fresh.goto = AsyncMock(return_value=MagicMock(status=200))

        async def open_game() -> None:
            self.session.context.pages.append(fresh)

        self.button.click.side_effect = open_game
        self.assertTrue(await self.session._resume_from_lobby())
        self.assertIs(self.session.page, fresh)
        self.game.goto.assert_not_awaited()

    async def test_pending_manual_window_keeps_retrying_authenticated_lobby(self) -> None:
        self.session.config = replace(self.config, headless=False, manual_login_timeout_seconds=60)
        self.session._find_authenticated_page = AsyncMock(return_value=None)
        clock = [0.0]

        async def advance(seconds: float) -> None:
            clock[0] += seconds

        async def resume() -> bool:
            self.session.state = SessionState.READY
            self.session.authentication_generation += 1
            return True

        with patch('ogame_bot.core.session.time', SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(self.session, '_interruptible_wait', side_effect=advance), \
                patch.object(self.session, '_resume_from_lobby', side_effect=resume) as retry:
            await self.session._wait_for_manual_login()
        retry.assert_awaited_once()
        self.assertEqual(clock[0], 15)
        self.assertEqual(self.session.state, SessionState.READY)

    async def test_expired_game_recovers_through_delayed_lobby_and_new_tab(self) -> None:
        identity = MagicMock()
        identity.count = AsyncMock(return_value=0)
        identity.is_visible = AsyncMock(return_value=False)
        identity.wait_for = AsyncMock(side_effect=PlaywrightTimeout('synthetic expired game'))
        other = MagicMock()
        self.page.locator.side_effect = lambda selector: identity if selector == '#playerName' else other
        self.session.context.pages = [self.page]
        self.session.state = SessionState.READY
        self.link.count.return_value = 0

        async def navigate(url: str, **kwargs: object) -> MagicMock:
            self.page.url = url
            return MagicMock(status=200)

        async def hydrate(**kwargs: object) -> None:
            self.link.count.return_value = 1

        async def accounts() -> None:
            self.page.url = 'https://lobby.ogame.gameforge.com/es_ES/accounts'

        async def open_game() -> None:
            self.session.context.pages.append(self.game)

        self.page.goto = AsyncMock(side_effect=navigate)
        self.link.wait_for.side_effect = hydrate
        self.link.click.side_effect = accounts
        self.button.click.side_effect = open_game
        await self.session.check()
        self.assertEqual(self.session.state, SessionState.READY)
        self.assertIs(self.session.page, self.game)
        self.assertEqual(self.session.authentication_generation, 1)
        self.link.wait_for.assert_awaited_once()
        self.button.click.assert_awaited_once()
        self.assertEqual(self.page.goto.await_count, 2)

    async def test_running_bot_releases_control_between_failed_reconnection_attempts(self) -> None:
        self.page.locator.return_value.wait_for = AsyncMock()
        with patch.object(self.session, '_navigate', new_callable=AsyncMock), \
                patch.object(self.session, '_identity', new_callable=AsyncMock, return_value=False), \
                patch.object(self.session, '_resume_from_lobby', new_callable=AsyncMock, return_value=False), \
                patch.object(self.session, '_wait_for_manual_login', new_callable=AsyncMock) as manual:
            with self.assertRaises(LoginRequired):
                await self.session.check()
        manual.assert_not_awaited()
        self.assertFalse(self.session.action_lock.locked())
        self.assertEqual(self.session.state, SessionState.WAITING_LOGIN)

    async def test_stale_tab_failure_does_not_abort_wait_for_late_popup(self) -> None:
        fresh = MagicMock()
        fresh.url = self.config.overview_url
        fresh.is_closed.return_value = False
        fresh.locator.return_value.count = AsyncMock(return_value=1)
        fresh.locator.return_value.is_visible = AsyncMock(return_value=True)
        fresh.locator.return_value.inner_text = AsyncMock(return_value=self.config.expected_player_name)
        fresh.locator.return_value.wait_for = AsyncMock()
        fresh.goto = AsyncMock(return_value=MagicMock(status=200))

        async def expire(url: str, **kwargs: object) -> MagicMock:
            self.game.locator.return_value.count.return_value = 0
            return MagicMock(status=200)

        async def popup(seconds: float) -> None:
            self.session.context.pages.append(fresh)

        self.game.goto.side_effect = expire
        with patch.object(self.session, '_interruptible_wait', side_effect=popup):
            self.assertTrue(await self.session._resume_from_lobby())
        self.assertIs(self.session.page, fresh)
        self.game.goto.assert_awaited_once()
        self.button.click.assert_awaited_once()

    async def test_another_universe_is_not_an_authenticated_target(self) -> None:
        self.game.url = 'https://s278-es.ogame.gameforge.com/game/index.php?page=ingame&component=overview'
        self.assertIsNone(await self.session._find_authenticated_page())
        with patch('ogame_bot.core.session.time', SimpleNamespace(monotonic=MagicMock(side_effect=[0, 31]))):
            self.assertFalse(await self.session._resume_from_lobby())
        self.assertNotEqual(self.session.state, SessionState.READY)
