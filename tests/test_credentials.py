"""Synthetic credentials only; native smoke test uses an isolated random key."""
from __future__ import annotations

import ctypes
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from ogame_bot.config import load_config
from ogame_bot.credentials import CredentialError, LoginCredentials, WindowsCredentialStore, _Credential, credential_target, main, set_auto_login
from ogame_bot.errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]
SECRET = LoginCredentials('synthetic@example.invalid', 'Synthetic-only-pässword')


class FakeWindowsAPI:
    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}
        self.freed = 0
        self.buffers: list[object] = []

    def CredWriteW(self, pointer: object, flags: int) -> bool:
        value = ctypes.cast(pointer, ctypes.POINTER(_Credential)).contents
        self.items[value.TargetName] = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize)
        return True

    def CredReadW(self, target: str, kind: int, flags: int, output: object) -> bool:
        if target not in self.items:
            ctypes.set_last_error(1168)
            return False
        payload = self.items[target]
        buffer = ctypes.create_string_buffer(payload, len(payload))
        value = _Credential()
        value.CredentialBlobSize = len(payload)
        value.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        pointer = ctypes.pointer(value)
        self.buffers.extend([buffer, value, pointer])
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(_Credential)))[0] = pointer
        return True

    def CredFree(self, pointer: object) -> None:
        self.freed += 1

    def CredDeleteW(self, target: str, kind: int, flags: int) -> bool:
        self.items.pop(target, None)
        return True


class CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        # The fake Win32 API must also run in Linux CI, where ctypes does not
        # expose the native Windows last-error functions.
        last_error = [0]
        getter = patch.object(ctypes, 'get_last_error', lambda: last_error[0], create=True)
        setter = patch.object(ctypes, 'set_last_error', lambda value: last_error.__setitem__(0, value), create=True)
        getter.start()
        setter.start()
        self.addCleanup(getter.stop)
        self.addCleanup(setter.stop)
        self.config = load_config(ROOT / 'config.example.yaml')
        self.api = FakeWindowsAPI()
        self.store = WindowsCredentialStore(self.api)
        self.target = credential_target(self.config)

    def test_default_config_does_not_enable_password_login(self) -> None:
        self.assertFalse(self.config.auto_login)
        self.assertEqual(self.config.login_retry_seconds, 300)

    def test_fake_windows_api_roundtrip_and_delete(self) -> None:
        self.assertIsNone(self.store.read(self.target))
        self.store.write(self.target, SECRET)
        self.assertEqual(self.store.read(self.target), SECRET)
        self.assertEqual(self.api.freed, 1)
        self.store.delete(self.target)
        self.assertIsNone(self.store.read(self.target))

    def test_secret_objects_do_not_print_email_or_password(self) -> None:
        self.assertNotIn(SECRET.email, repr(SECRET))
        self.assertNotIn(SECRET.password, repr(SECRET))

    def test_target_is_bound_to_account_and_contains_no_email(self) -> None:
        from dataclasses import replace
        self.assertNotEqual(self.target, credential_target(replace(self.config, expected_player_name='Another commander')))
        self.assertNotIn(self.config.expected_player_name, self.target)

    def test_malformed_blob_is_rejected_and_freed(self) -> None:
        for payload in (b'not-json', b'{"version":1,"password":"secret"}', b'{}'):
            self.api.items[self.target] = payload
            with self.assertRaises(CredentialError):
                self.store.read(self.target)
        self.assertEqual(self.api.freed, 3)

    def test_invalid_target_and_oversized_credentials_are_not_written(self) -> None:
        with self.assertRaises(CredentialError):
            self.store.write('another-application', SECRET)
        with self.assertRaises(CredentialError):
            self.store.write(self.target, LoginCredentials(SECRET.email, 'a' * 3000))
        self.assertEqual(self.api.items, {})

    def test_windows_access_denial_has_no_plaintext_fallback_or_secret_output(self) -> None:
        ctypes.set_last_error(5)
        with patch.object(self.api, 'CredWriteW', return_value=False):
            with self.assertRaises(CredentialError) as error:
                self.store.write(self.target, SECRET)
        self.assertIn('sesión normal de Windows', str(error.exception))
        self.assertNotIn(SECRET.password, str(error.exception))
        self.assertEqual(self.api.items, {})

    def test_local_wizard_stores_credentials_and_enables_flag_without_writing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_bytes((ROOT / 'config.example.yaml').read_bytes())
            with patch('sys.argv', ['credentials', 'configure', '--config', str(path)]), \
                    patch('ogame_bot.credentials.credential_store') as store, \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', return_value=SECRET.email), \
                    patch('getpass.getpass', return_value=SECRET.password), patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(), 0)
            store.return_value.write.assert_called_once_with(credential_target(load_config(path)), SECRET)
            self.assertTrue(load_config(path).auto_login)
            self.assertNotIn(SECRET.password, path.read_text(encoding='utf-8'))
            self.assertNotIn(SECRET.email, path.read_text(encoding='utf-8'))
            self.assertNotIn(SECRET.password, output.getvalue())

    def test_configuration_only_contains_flags_and_validates_new_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_bytes((ROOT / 'config.example.yaml').read_bytes())
            set_auto_login(path, True)
            config = load_config(path)
            self.assertTrue(config.auto_login)
            self.assertNotIn(SECRET.password, path.read_text(encoding='utf-8'))
            raw = yaml.safe_load(path.read_text(encoding='utf-8'))
            raw['session']['login_retry_seconds'] = 1
            path.write_text(yaml.safe_dump(raw), encoding='utf-8')
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_secret_configuration_fields_are_rejected(self) -> None:
        for key in ('password', 'email', 'token'):
            with tempfile.TemporaryDirectory() as directory:
                raw = yaml.safe_load((ROOT / 'config.example.yaml').read_text(encoding='utf-8'))
                raw['session'][key] = 'synthetic-only'
                path = Path(directory) / 'config.yaml'
                path.write_text(yaml.safe_dump(raw), encoding='utf-8')
                with self.assertRaises(ConfigurationError):
                    load_config(path)

    def test_cli_rejects_noninteractive_configuration_without_reading_password(self) -> None:
        with patch('sys.argv', ['credentials', 'configure', '--config', str(ROOT / 'config.example.yaml')]), \
                patch('ogame_bot.credentials.credential_store') as store, \
                patch('sys.stdin.isatty', return_value=False), \
                patch('getpass.getpass') as password, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(), 1)
        password.assert_not_called()
        store.return_value.write.assert_not_called()

    def test_cli_mismatched_passwords_are_not_stored(self) -> None:
        with patch('sys.argv', ['credentials', 'configure', '--config', str(ROOT / 'config.example.yaml')]), \
                patch('ogame_bot.credentials.credential_store') as store, \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value=SECRET.email), \
                patch('getpass.getpass', side_effect=['first', 'second']), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(), 1)
        store.return_value.write.assert_not_called()

    def test_status_output_does_not_expose_credentials(self) -> None:
        with patch('sys.argv', ['credentials', 'status', '--config', str(ROOT / 'config.example.yaml')]), \
                patch('ogame_bot.credentials.credential_store') as store, patch('sys.stdout', new_callable=io.StringIO) as output:
            store.return_value.read.return_value = SECRET
            self.assertEqual(main(), 0)
        self.assertNotIn(SECRET.password, output.getvalue())
        self.assertNotIn(SECRET.email, output.getvalue())


@unittest.skipUnless(os.name == 'nt' and os.environ.get('OGAME_CREDENTIAL_TESTS') == '1', 'Native synthetic Credential Manager test is opt-in')
class NativeCredentialTests(unittest.TestCase):
    def test_isolated_synthetic_credential_roundtrip(self) -> None:
        store = WindowsCredentialStore()
        target = 'OrbitOGame/Lobby/v1/test-' + uuid.uuid4().hex
        saved = False
        try:
            store.write(target, SECRET)
            saved = True
            self.assertEqual(store.read(target), SECRET)
        finally:
            if saved:
                store.delete(target)
        self.assertIsNone(store.read(target))
