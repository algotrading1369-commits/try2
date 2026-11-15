"""Opt-in form login; credentials never leave official Gameforge origins.

Unknown forms, registration, social login and verification challenges require
manual access. There is no CAPTCHA solver, trace, screenshot or HTML dump here.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error, Frame, Locator, Page

from ..credentials import CredentialError, credential_store, credential_target

if TYPE_CHECKING:
    from .session import SessionManager

LOGIN = re.compile(r'^(?:Iniciar sesi[oó]n|Acceder|Entrar|Log ?in|Sign in)$', re.IGNORECASE)
OFFICIAL_HOSTS = {'lobby.ogame.gameforge.com', 'account.gameforge.com', 'gameforge.com'}


def official_login_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.netloc in OFFICIAL_HOSTS


async def visible(locator: Locator) -> bool:
    return await locator.count() == 1 and await locator.is_visible()


async def verification_required(page: Page) -> bool:
    for frame in page.frames:
        if not official_login_url(frame.url):
            continue
        markers = frame.locator('iframe[src*="captcha" i], [id*="captcha" i]:not([class*="badge" i]), [class*="captcha" i]:not([class*="badge" i]), input[autocomplete="one-time-code"], input[name="otp"], input[name="code"]')
        for marker in (await markers.all())[:30]:
            if await marker.is_visible():
                return True
        prompt = frame.get_by_text(re.compile(r'c[oó]digo de verificaci[oó]n|verification code|verifica tu identidad|verify your identity|two.factor authentication', re.IGNORECASE))
        if await prompt.count() and await prompt.first.is_visible():
            return True
    return False


async def login_form(page: Page) -> tuple[Frame, Locator, Locator, Locator, Locator] | None:
    matches: list[tuple[Frame, Locator, Locator, Locator, Locator]] = []
    for frame in page.frames:
        if not official_login_url(frame.url):
            continue
        forms = frame.locator('form').filter(has=frame.locator('input[type="password"]:visible'))
        for form in (await forms.all())[:10]:
            email = form.locator('input[type="email"]:visible, input[name="email"]:visible, input[autocomplete="username"]:visible')
            password = form.locator('input[type="password"]:visible')
            submit = form.get_by_role('button', name=LOGIN)
            if await visible(email) and await visible(password) and await visible(submit) and await submit.is_enabled():
                action = await form.get_attribute('action')
                if official_login_url(urljoin(frame.url, action or frame.url)):
                    matches.append((frame, form, email, password, submit))
    return matches[0] if len(matches) == 1 else None


class LobbyAuthenticator:
    MAX_ATTEMPTS = 3

    def __init__(self) -> None:
        self.attempts = 0
        self.next_attempt = 0.0
        self.halted = False
        self.status = 'disabled'

    def _pause(self, status: str) -> bool:
        self.halted = True
        self.status = status
        return False

    async def attempt(self, session: SessionManager) -> bool:
        config, page = session.config, session.page
        if not config.auto_login:
            self.status = 'disabled'
            return False
        if self.halted:
            return False
        if self.attempts >= self.MAX_ATTEMPTS:
            return self._pause('login_attempt_limit')
        if time.monotonic() < self.next_attempt:
            self.status = 'login_cooldown'
            return False
        if page is None or not official_login_url(page.url):
            self.status = 'login_origin_not_allowed'
            return False
        try:
            if await verification_required(page):
                return self._pause('verification_required')
            contract = await login_form(page)
            if contract is None:
                # A landing page may require opening the Login tab first.
                for role in ('tab', 'button', 'link'):
                    opener = page.get_by_role(role, name=LOGIN)
                    if await visible(opener):
                        await session._interruptible_read(opener.click())
                        await session._interruptible_wait(1)
                        break
                if not official_login_url(page.url) or await verification_required(page):
                    return self._pause('verification_or_external_login_required')
                contract = await login_form(page)
            if contract is None:
                self.status = 'login_form_not_recognized'
                return False
            frame, form, email, password, submit = contract

            def trusted_frame() -> bool:
                return official_login_url(page.url) and official_login_url(frame.url)

            async def trusted_form() -> bool:
                action = await form.get_attribute('action')
                return trusted_frame() and official_login_url(urljoin(frame.url, action or frame.url))

            if not await trusted_form():
                return self._pause('login_origin_changed')
            try:
                credentials = credential_store().read(credential_target(config))
            except CredentialError:
                self.status = 'credential_store_unavailable'
                return False
            if credentials is None:
                self.status = 'credentials_not_configured'
                return False
            self.attempts += 1
            self.next_attempt = time.monotonic() + config.login_retry_seconds
            self.status = 'login_submitted'
            # Do not pass fill exceptions to the logger: Playwright can include
            # the supplied value in its call history.
            try:
                await email.fill(credentials.email)
                if not await trusted_form() or await verification_required(page):
                    return self._pause('verification_or_origin_changed')
                await password.fill(credentials.password)
                if not await trusted_form() or await verification_required(page):
                    return self._pause('verification_or_origin_changed')
                await session._interruptible_read(submit.click())
            finally:
                del credentials
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                session._check_open()
                if not official_login_url(page.url):
                    return self._pause('login_redirect_not_allowed')
                if await verification_required(page):
                    return self._pause('verification_required')
                if await visible(page.get_by_role('link', name='Jugar', exact=True)):
                    self.status = 'authenticated'
                    return True
                for current in page.frames:
                    if not official_login_url(current.url):
                        continue
                    alerts = current.locator('[role="alert"], .error, .error-message')
                    for alert in (await alerts.all())[:20]:
                        if await alert.is_visible() and (await alert.inner_text()).strip():
                            return self._pause('login_rejected_or_verification_required')
                await session._interruptible_wait(1)
            self.status = 'login_result_timeout'
            return False
        except Error:
            self.status = 'login_interface_error'
            return False
