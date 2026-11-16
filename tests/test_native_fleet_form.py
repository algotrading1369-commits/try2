"""Two-step expedition DOM, journal-before-send and strict final checks."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ogame_bot.errors import ReadError
from ogame_bot.fleet.form import INPUT_NAMES, is_native_form, parse_quote
from ogame_bot.scanner.dom import Document
import test_fleet as fixtures


class QuoteTests(unittest.TestCase):
    def test_observed_visible_quote(self):
        self.assertEqual(parse_quote('33 (1%)','75.000','0:13:29 h'),(33,75000,809))
        self.assertEqual(parse_quote('23 (0%)','75.000','0:04:12 h'),(23,75000,252))
        self.assertEqual(parse_quote('1.234 (1,5%)','1.000.000','12:59:59 h'),(1234,1000000,46799))

    def test_loading_rounded_or_impossible_quote_is_rejected(self):
        for fuel,capacity,duration in [('Cargando','75000','0:04:12 h'),('23','75 K','0:04:12 h'),('23','75000','0:60:00 h'),('23','75000','0:00:00 h')]:
            with self.subTest(quote=(fuel,capacity,duration)), self.assertRaises(ReadError):
                parse_quote(fuel,capacity,duration)

    def test_templates_are_not_the_fleet_form(self):
        self.assertFalse(is_native_form(Document('<div id="templates"><input name="transporterLarge"></div>')))


class NativeTransactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse the transaction harness, not its legacy-form test cases.
        self.h = fixtures.FleetTransactionTests('test_send_is_persisted_and_confirmed_by_new_movement')
        self.h.setUp()
        self.addCleanup(self.h.temp.cleanup)
        self.addCleanup(self.h.journal.close)
        h = self.h
        self.display_target = '['+h.flight.destination+'] Espacio infinito'
        h.page.wait_for_function = AsyncMock()
        original = h.page.locator.side_effect
        focused = [None]
        self.internal_coordinates = {}

        async def key_press(key):
            selector = focused[0]
            if selector is None:
                raise AssertionError('Keyboard input without a focused coordinate')
            control = h.controls[selector]
            if key == 'Backspace':
                control.value = ''
            elif key.isdecimal():
                control.value += key
                self.internal_coordinates[selector[1:]] = control.value
            elif key not in {'ControlOrMeta+A','Tab'}:
                raise AssertionError('Unexpected coordinate key')
        h.page.keyboard.press = AsyncMock(side_effect=key_press)

        def locator(selector):
            control = original(selector)
            control.press = AsyncMock()
            control.wait_for = AsyncMock()
            if selector in {'#galaxy','#system','#position'}:
                async def focus():
                    focused[0] = selector
                control.click = AsyncMock(side_effect=focus)
            if selector == '#missionButton15[data-mission="15"]':
                async def ready(**kwargs):
                    route = ':'.join(self.internal_coordinates.get(key,'') for key in ('galaxy','system','position'))
                    if route != h.flight.destination:
                        raise PlaywrightTimeout('The game target was not updated by keyboard events')
                control.wait_for = AsyncMock(side_effect=ready)
            if selector == '#pbutton':
                control.get_attribute = AsyncMock(return_value='planet_selected')
            if selector == '#speedPercentage':
                control.get_attribute = AsyncMock(return_value='10')
            text = {'#speedPercentage .step.selected':'100','#missionName':'Expedición',
                    '#consumption':'100 (0%)','#storage':'200000000','#duration':'0:01:00 h',
                    '#targetPlanet':self.display_target}
            if selector in text:
                control.inner_text = AsyncMock(return_value=text[selector])
            if selector == 'select#expeditiontime':
                async def select(value, **kwargs):
                    control.value = value
                control.select_option = AsyncMock(side_effect=select)
            return control
        h.page.locator.side_effect = locator

        def document():
            source = fixtures.rendered(h.flight,sent=h.sent and h.confirm_visible)
            source = source.replace('<input name="am203" value="999">','')
            fields = ''.join(f'<input name="{name}">' for name in INPUT_NAMES.values())
            return source+f'<div id="technologies">{fields}</div>'
        h.page.content = AsyncMock(side_effect=document)

    async def test_native_form_reaches_one_durable_confirmed_send(self):
        h = self.h
        result = await h.executor.execute(h.flight)
        self.assertEqual(result['state'],'confirmed')
        self.assertEqual(result['fleet_id'],'77')
        self.assertTrue(h.sent)
        h.controls[h.config.fleet.selectors['send']].click.assert_awaited_once()
        self.assertNotIn(h.config.fleet.selectors['ship_input'].format(id=203),h.controls)
        self.assertNotIn(h.config.fleet.selectors['continue_3'],h.controls)
        self.assertEqual(':'.join(self.internal_coordinates[key] for key in ('galaxy','system','position')),h.flight.destination)

    async def test_changed_food_blocks_before_preparing_or_sending(self):
        h = self.h
        h.page.locator('input#food[name="food"]').value = '1'
        with self.assertRaises(ReadError):
            await h.executor.execute(h.flight)
        self.assertFalse(h.sent)
        self.assertIsNone(h.journal.pending('fleet'))

    async def test_extra_ship_is_rejected_before_send(self):
        h = self.h
        control = h.page.locator('#technologies input[name="fighterLight"]')
        control.input_value = AsyncMock(return_value='1')
        with self.assertRaises(ReadError):
            await h.executor.execute(h.flight)
        self.assertFalse(h.sent)
        self.assertIsNone(h.journal.pending('fleet'))

    async def test_ajax_timeout_becomes_a_read_error_before_any_send(self):
        h = self.h
        h.page.wait_for_function.side_effect = PlaywrightTimeout('Synthetic timeout')
        with self.assertRaises(ReadError):
            await h.executor.execute(h.flight)
        self.assertFalse(h.sent)
        self.assertIsNone(h.journal.pending('fleet'))

    async def test_stale_internal_target_is_rejected_despite_matching_input_values(self):
        h = self.h
        self.display_target = '[1:999:16] Espacio infinito'
        with self.assertRaises(ReadError):
            await h.executor.execute(h.flight)
        self.assertFalse(h.sent)
        self.assertIsNone(h.journal.pending('fleet'))
