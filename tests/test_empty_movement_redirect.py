"""Observed empty-movement redirect, without weakening fleet baselines."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ogame_bot.config import load_config
from ogame_bot.core.session import SessionManager, SessionState
from ogame_bot.errors import ReadError
from ogame_bot.fleet.parsing import outgoing, verified_empty_dispatch
from ogame_bot.scanner.dom import Document


EMPTY = '''<div id="eventboxBlank" class="textCenter" style="">No hay movimientos de flota.</div>
<div id="eventboxFilled" class="eventToggle" style="display: none;"></div>
<div id="slots" class="fleft"><div><span><span>Flotas:</span>0/3</span></div>
<div><span><span>Expediciones:</span>0/1</span></div></div>
<div id="slots"><div class="tactical_retreat">Retirada táctica: 5:1</div></div>
<div id="technologies"></div><a id="continueToFleet2">SIGUIENTE</a>'''


class EmptyMovementParsingTests(unittest.TestCase):
    def test_observed_empty_dispatch_has_a_verified_empty_baseline(self):
        reading = outgoing(Document(EMPTY))
        self.assertEqual(reading.status, 'complete')
        self.assertEqual(reading.data, [])

    def test_absent_loading_hidden_or_contradictory_markers_are_not_empty(self):
        variants = [
            EMPTY.replace('0/3','1/3'), EMPTY.replace('0/1','1/1'),
            EMPTY.replace('0/3','Cargando'),
            EMPTY.replace('id="eventboxBlank"','id="other"'),
            EMPTY.replace('class="textCenter" style=""','class="textCenter" style="display:none"'),
            EMPTY.replace('class="eventToggle" style="display: none;"','class="eventToggle" style=""'),
            EMPTY.replace('id="technologies"','id="other"'),
            EMPTY+'<div class="fleetDetails"></div>',
            EMPTY+'<div class="eventFleet"></div>',
            EMPTY+'<div data-fleet-id="123"></div>',
            EMPTY+'<div id="slots">Flotas: 0/3</div>',
            EMPTY+'<div id="eventboxBlank">No hay movimientos de flota.</div>',
        ]
        for html in variants:
            with self.subTest(html=html):
                self.assertFalse(verified_empty_dispatch(Document(html)))
                self.assertNotEqual(outgoing(Document(html)).status,'complete')


class EmptyMovementSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cfg = load_config(Path(__file__).resolve().parents[1]/'config.example.yaml')
        self.page = MagicMock()
        self.page.url = cfg.server_origin+'/game/index.php?page=ingame&component=fleetdispatch'
        self.page.is_closed.return_value = False
        self.page.content = AsyncMock(return_value=EMPTY)
        control = self.page.locator.return_value
        control.wait_for = AsyncMock()
        control.filter.return_value.first.wait_for = AsyncMock()
        context = MagicMock(pages=[self.page])
        self.session = SessionManager(context,cfg)
        self.session.page = self.page
        self.session.state = SessionState.READY
        self.session._check_open = MagicMock()
        self.session._navigate = AsyncMock()
        self.session._identity = AsyncMock(return_value=True)

    async def test_movement_redirect_is_accepted_only_with_verified_zero_fleets(self):
        async with self.session.read_page('movement') as page:
            self.assertIs(page,self.page)
        self.session._identity.assert_awaited_once()

    async def test_nonempty_or_unknown_redirect_is_still_rejected(self):
        for html in (EMPTY.replace('0/3','1/3'),'<div>Cargando</div>'):
            self.page.content.return_value = html
            with self.assertRaises(ReadError):
                async with self.session.read_page('movement'):
                    self.fail('An unverified baseline must not authorize a send')

    async def test_other_component_redirect_remains_an_error(self):
        with self.assertRaises(ReadError):
            async with self.session.read_page('research'):
                self.fail('Only movement can use the empty dispatch redirect')
