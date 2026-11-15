"""Bounded Lobby reentry using the observed ES account-row contract."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from ogame_bot.config import load_config
from ogame_bot.core.session import SessionManager, SessionState
from ogame_bot.errors import IdentityMismatch


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

    async def test_login_page_without_play_requires_manual_access(self) -> None:
        self.page.url = 'https://lobby.ogame.gameforge.com/es_ES/'
        self.link.count.return_value = 0
        self.assertFalse(await self.session._resume_from_lobby())
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
        self.assertFalse(await self.session._resume_from_lobby())
        self.assertNotEqual(self.session.state, SessionState.READY)
        self.assertEqual(self.session.authentication_generation, 0)

    async def test_another_universe_is_not_an_authenticated_target(self) -> None:
        self.game.url = 'https://s278-es.ogame.gameforge.com/game/index.php?page=ingame&component=overview'
        self.assertIsNone(await self.session._find_authenticated_page())
        with patch('ogame_bot.core.session.time', SimpleNamespace(monotonic=MagicMock(side_effect=[0, 31]))):
            self.assertFalse(await self.session._resume_from_lobby())
        self.assertNotEqual(self.session.state, SessionState.READY)
