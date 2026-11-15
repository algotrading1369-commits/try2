"""Password login uses synthetic credentials and mocked official forms only."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import Error

from ogame_bot.config import load_config
from ogame_bot.core.lobby_auth import LobbyAuthenticator, login_form, official_login_url
from ogame_bot.credentials import CredentialError, LoginCredentials

ROOT = Path(__file__).resolve().parents[1]
SECRET = LoginCredentials('synthetic@example.invalid', 'not-a-real-password')


def locator() -> MagicMock:
    item = MagicMock()
    item.count = AsyncMock(return_value=1)
    item.is_visible = AsyncMock(return_value=True)
    item.is_enabled = AsyncMock(return_value=True)
    item.fill = AsyncMock()
    item.click = AsyncMock()
    item.get_attribute = AsyncMock(return_value='')
    item.all = AsyncMock(return_value=[])
    return item


class LoginOriginTests(unittest.TestCase):
    def test_only_explicit_https_official_hosts_are_allowed(self) -> None:
        self.assertTrue(official_login_url('https://lobby.ogame.gameforge.com/es_ES/'))
        self.assertTrue(official_login_url('https://account.gameforge.com/'))
        for url in ('http://gameforge.com/', 'https://gameforge.com.evil.example/', 'https://gameforge.com@evil.example/', 'https://gameforge.com:8443/', 'https://other.gameforge.com/'):
            self.assertFalse(official_login_url(url))


class LobbyAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.auth = LobbyAuthenticator()
        self.page = MagicMock()
        self.page.url = 'https://lobby.ogame.gameforge.com/es_ES/'
        self.frame = MagicMock()
        self.frame.url = self.page.url
        self.frame.locator.return_value.all = AsyncMock(return_value=[])
        self.page.frames = [self.frame]
        self.form, self.email, self.password, self.submit = (locator() for _ in range(4))
        self.play = locator()
        self.page.get_by_role.return_value = self.play
        self.session = SimpleNamespace(config=replace(load_config(ROOT / 'config.example.yaml'), auto_login=True),
            page=self.page, _check_open=MagicMock(), _interruptible_wait=AsyncMock())

        async def interrupt(operation):
            return await operation

        self.session._interruptible_read = interrupt
        self.contract = patch('ogame_bot.core.lobby_auth.login_form', new_callable=AsyncMock,
            return_value=(self.frame, self.form, self.email, self.password, self.submit))
        self.contract_mock = self.contract.start()
        self.addCleanup(self.contract.stop)
        self.challenge = patch('ogame_bot.core.lobby_auth.verification_required', new_callable=AsyncMock, return_value=False)
        self.challenge_mock = self.challenge.start()
        self.addCleanup(self.challenge.stop)
        self.store = patch('ogame_bot.core.lobby_auth.credential_store')
        self.store_mock = self.store.start()
        self.addCleanup(self.store.stop)
        self.store_mock.return_value.read.return_value = SECRET

    async def test_success_uses_one_official_form_and_never_logs_values(self) -> None:
        with self.assertNoLogs('ogame_bot'):
            self.assertTrue(await self.auth.attempt(self.session))
        self.email.fill.assert_awaited_once_with(SECRET.email)
        self.password.fill.assert_awaited_once_with(SECRET.password)
        self.submit.click.assert_awaited_once()
        self.assertEqual(self.auth.status, 'authenticated')

    async def test_disabled_login_never_accesses_credential_store(self) -> None:
        self.session.config = replace(self.session.config, auto_login=False)
        self.assertFalse(await self.auth.attempt(self.session))
        self.store_mock.assert_not_called()

    async def test_successful_renewal_resets_consecutive_failure_budget(self) -> None:
        self.auth.attempts = 2
        self.assertTrue(await self.auth.attempt(self.session))
        self.assertEqual(self.auth.attempts, 0)
        self.auth.next_attempt = 0
        self.assertTrue(await self.auth.attempt(self.session))
        self.assertEqual(self.submit.click.await_count, 2)

    async def test_untrusted_page_never_reads_or_fills_credentials(self) -> None:
        self.page.url = 'https://evil.example/'
        self.assertFalse(await self.auth.attempt(self.session))
        self.store_mock.assert_not_called()
        self.email.fill.assert_not_awaited()

    async def test_untrusted_form_action_never_reads_credentials(self) -> None:
        self.form.get_attribute.return_value = 'https://evil.example/collect'
        self.assertFalse(await self.auth.attempt(self.session))
        self.store_mock.assert_not_called()

    async def test_origin_change_after_email_fill_prevents_password_fill(self) -> None:
        async def redirect(value: str) -> None:
            self.frame.url = 'https://evil.example/'

        self.email.fill.side_effect = redirect
        self.assertFalse(await self.auth.attempt(self.session))
        self.password.fill.assert_not_awaited()
        self.submit.click.assert_not_awaited()

    async def test_challenge_prevents_credential_access_and_submission(self) -> None:
        self.challenge_mock.return_value = True
        self.assertFalse(await self.auth.attempt(self.session))
        self.assertTrue(self.auth.halted)
        self.store_mock.assert_not_called()

    async def test_challenge_after_submission_pauses_future_attempts(self) -> None:
        self.challenge_mock.side_effect = [False, False, False, True]
        self.assertFalse(await self.auth.attempt(self.session))
        self.assertEqual(self.auth.status, 'verification_required')
        self.assertFalse(await self.auth.attempt(self.session))
        self.submit.click.assert_awaited_once()

    async def test_rejection_halts_without_logging_server_error_or_password(self) -> None:
        self.play.count.return_value = 0
        alert = locator()
        alert.inner_text = AsyncMock(return_value='Rejected: ' + SECRET.password)
        self.frame.locator.return_value.all.return_value = [alert]
        with self.assertNoLogs('ogame_bot'):
            self.assertFalse(await self.auth.attempt(self.session))
        self.assertTrue(self.auth.halted)
        self.assertFalse(await self.auth.attempt(self.session))
        self.submit.click.assert_awaited_once()

    async def test_fill_exception_containing_secret_is_sanitized(self) -> None:
        self.password.fill.side_effect = Error('Call log fill: ' + SECRET.password)
        with self.assertNoLogs('ogame_bot'):
            self.assertFalse(await self.auth.attempt(self.session))
        self.assertEqual(self.auth.status, 'login_interface_error')
        self.assertNotIn(SECRET.password, self.auth.status)

    async def test_missing_credentials_and_unavailable_store_never_fill(self) -> None:
        self.store_mock.return_value.read.return_value = None
        self.assertFalse(await self.auth.attempt(self.session))
        self.email.fill.assert_not_awaited()
        self.store_mock.return_value.read.side_effect = CredentialError('Unavailable')
        self.assertFalse(await self.auth.attempt(self.session))
        self.email.fill.assert_not_awaited()

    async def test_cooldown_and_attempt_limit_do_not_submit_again(self) -> None:
        self.assertTrue(await self.auth.attempt(self.session))
        self.assertFalse(await self.auth.attempt(self.session))
        self.assertEqual(self.auth.status, 'login_cooldown')
        self.auth.attempts = 3
        self.assertFalse(await self.auth.attempt(self.session))
        self.assertEqual(self.auth.status, 'login_attempt_limit')
        self.submit.click.assert_awaited_once()

    async def test_unknown_form_never_loads_credentials(self) -> None:
        self.contract_mock.return_value = None
        self.page.get_by_role.return_value.count.return_value = 0
        self.assertFalse(await self.auth.attempt(self.session))
        self.store_mock.assert_not_called()
        self.assertEqual(self.auth.status, 'login_form_not_recognized')


class FormContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_and_external_form_action_are_rejected(self) -> None:
        page, frame = MagicMock(), MagicMock()
        frame.url = 'https://lobby.ogame.gameforge.com/es_ES/'
        page.frames = [frame]
        form = locator()
        fields = locator()
        submit = locator()
        form.locator.return_value = fields
        form.get_by_role.return_value = submit
        frame.locator.return_value.filter.return_value.all = AsyncMock(return_value=[form])
        submit.count.return_value = 0  # Registrarse does not match the Login label
        self.assertIsNone(await login_form(page))
        submit.count.return_value = 1
        form.get_attribute.return_value = 'https://evil.example/collect'
        self.assertIsNone(await login_form(page))
        form.get_attribute.return_value = '/login'
        self.assertIsNotNone(await login_form(page))
