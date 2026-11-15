"""Ubuntu CI only: open a local fixture on Xvfb with the real BrowserManager.

No SessionManager, login, network navigation or real browser profile is used.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ogame_bot.config import load_config
from ogame_bot.core.browser import BrowserManager


async def smoke() -> None:
    config = load_config(Path('/var/lib/ogame/config.yaml'))
    with tempfile.TemporaryDirectory(prefix='ogame-browser-smoke-') as directory:
        root = Path(directory)
        config = replace(config, profile_dir=root / 'profile', headless=False, channel=None,
            scheduler=replace(config.scheduler, status_file=root / 'scheduler.json'))
        async with BrowserManager(config) as browser:
            page = await browser.require_context().new_page()
            await page.set_content('<title>ORBIT local fixture</title><h1>Browser ready</h1>')
            if await page.title() != 'ORBIT local fixture':
                raise RuntimeError('Local Chromium fixture failed')
    print('Chromium y Xvfb comprobados con una página local, sin acceso al juego.')


if __name__ == '__main__':
    asyncio.run(smoke())
