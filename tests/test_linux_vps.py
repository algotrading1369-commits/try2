"""VPS supervision, migration, private backups and systemd credential contracts."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from ogame_bot.config import load_config
from ogame_bot.credentials import CredentialError, LoginCredentials, credential_target
from ogame_bot.errors import CoreError
from ogame_bot.linux_credentials import CREDENTIAL_ID, SystemdCredentialStore
from ogame_bot.panel_service import PanelError
from ogame_bot.vps import VPSPanelService
from ogame_bot.vps_backup import make_backup
from ogame_bot.vps_config import prepare_config

ROOT = Path(__file__).resolve().parents[1]
SECRET = LoginCredentials('synthetic@example.invalid', 'synthetic-only-password')


class VPSConfigurationTests(unittest.TestCase):
    def test_migration_preserves_account_and_goals_but_resets_paths_and_live_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'config.yaml'
            prepare_config(ROOT / 'config.example.yaml', target)
            config = load_config(target)
            original = load_config(ROOT / 'config.example.yaml')
            self.assertEqual(config.expected_player_name, original.expected_player_name)
            self.assertEqual(config.brain.build_goals, original.brain.build_goals)
            self.assertFalse(config.auto_login)
            self.assertIsNone(config.channel)
            self.assertFalse(config.headless)
            self.assertTrue(config.brain.dry_run)
            self.assertFalse(config.brain.economic_live)
            self.assertEqual(config.brain.journal_file, Path(directory) / 'runtime' / 'orders.sqlite3')
            with self.assertRaises(CoreError):
                prepare_config(ROOT / 'config.example.yaml', target)


class VPSSupervisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'config.yaml'
        self.path.write_bytes((ROOT / 'config.example.yaml').read_bytes())
        self.service = VPSPanelService(self.path)
        self.children: list[MagicMock] = []

        def spawn(*args, **kwargs):
            child = MagicMock()
            child.pid = 900000 + len(self.children)
            child.poll.return_value = None
            self.children.append(child)
            return child

        popen = patch('ogame_bot.panel_service.subprocess.Popen', side_effect=spawn)
        self.popen = popen.start()
        self.addCleanup(popen.stop)
        self.clock = [100.0]
        clock = patch('ogame_bot.vps.time', SimpleNamespace(monotonic=lambda: self.clock[0]))
        clock.start()
        self.addCleanup(clock.stop)

    def test_first_install_waits_for_explicit_start(self) -> None:
        self.service.supervise_once()
        self.assertFalse(self.service.desired_running)
        self.popen.assert_not_called()

    def test_manual_stop_is_persisted_and_does_not_get_automatically_undone(self) -> None:
        self.service.start('scanner')
        self.service.stop()
        self.children[0].poll.return_value = 0
        self.clock[0] += 10000
        self.service.supervise_once()
        self.assertEqual(len(self.children), 1)
        restored = VPSPanelService(self.path)
        self.assertFalse(restored.desired_running)
        self.assertEqual(restored.desired_mode, 'scanner')

    def test_unexpected_zero_exit_is_restarted_after_backoff(self) -> None:
        self.service.start()
        self.children[0].poll.return_value = 0
        self.service.supervise_once()
        self.clock[0] += 4
        self.service.supervise_once()
        self.assertEqual(len(self.children), 1)
        self.clock[0] += 1
        self.service.supervise_once()
        self.assertEqual(len(self.children), 2)
        self.assertTrue(self.service.desired_running)

    def test_live_child_is_not_duplicated_and_bad_modes_are_rejected(self) -> None:
        self.service.start('session')
        self.service.supervise_once()
        with self.assertRaises(PanelError):
            self.service.start()
        self.assertEqual(len(self.children), 1)
        with self.assertRaises(PanelError):
            self.service.start('arbitrary-command')

    def test_shutdown_preserves_running_intent_for_reboot(self) -> None:
        self.service.start()
        self.service.shutdown_child()
        self.children[0].wait.assert_called_once_with(timeout=35)
        restored = VPSPanelService(self.path)
        self.assertTrue(restored.desired_running)

    def test_corrupt_intent_does_not_start_an_account(self) -> None:
        self.service.intent_path.parent.mkdir(parents=True, exist_ok=True)
        self.service.intent_path.write_text('{"running":"true","mode":"full"}')
        with self.assertRaises(CoreError):
            VPSPanelService(self.path)
        self.popen.assert_not_called()


class VPSBackupTests(unittest.TestCase):
    def test_archive_copies_consistent_orders_and_excludes_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / 'config.yaml'
            prepare_config(ROOT / 'config.example.yaml', config_path)
            config = load_config(config_path)
            config.brain.journal_file.parent.mkdir(parents=True)
            with closing(sqlite3.connect(config.brain.journal_file)) as connection:
                connection.execute('CREATE TABLE sample_orders (id INTEGER)')
                connection.execute('INSERT INTO sample_orders VALUES (1)')
                connection.commit()
            config.profile_dir.mkdir(parents=True)
            (config.profile_dir / 'Cookies').write_text('not-to-be-backed-up')
            archive = make_backup(config, root / 'backups', keep=1)
            with tarfile.open(archive) as data:
                self.assertEqual(set(data.getnames()), {'config.yaml', 'orders.sqlite3'})
                copy = root / 'copy.sqlite3'
                copy.write_bytes(data.extractfile('orders.sqlite3').read())
            with closing(sqlite3.connect(copy)) as check:
                self.assertEqual(check.execute('SELECT id FROM sample_orders').fetchone(), (1,))
            next_archive = make_backup(config, root / 'backups', keep=1)
            self.assertTrue(next_archive.exists())
            self.assertEqual(len(list((root / 'backups').glob('*.tar.gz'))), 1)

    def test_external_paths_are_not_added_to_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / 'config.yaml'
            prepare_config(ROOT / 'config.example.yaml', config_path)
            config = load_config(config_path)
            config = replace(config, scanner_output_file=root.parent / 'outside.json')
            with self.assertRaises(CoreError):
                make_backup(config, root / 'backups')


@unittest.skipUnless(os.name == 'posix', 'Linux file permissions and no-follow contract')
class LinuxCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = load_config(ROOT / 'config.example.yaml')
        self.target = credential_target(self.config)
        self.file = self.root / CREDENTIAL_ID
        self.file.write_text(json.dumps({'version': 1, 'target': self.target, 'email': SECRET.email, 'password': SECRET.password}))
        self.file.chmod(0o600)
        environment = patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = SystemdCredentialStore(self.root / 'sealed' / CREDENTIAL_ID)

    def test_runtime_read_is_account_bound_and_private(self) -> None:
        self.assertEqual(self.store.read(self.target), SECRET)
        with self.assertRaises(CredentialError):
            self.store.read(self.target+'x')
        self.file.chmod(0o644)
        with self.assertRaises(CredentialError):
            self.store.read(self.target)

    def test_no_plaintext_fallback_without_systemd_directory(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.store.read(self.target))

    def test_symlink_or_malformed_credential_is_rejected(self) -> None:
        self.file.unlink()
        private = self.root / 'other'
        private.write_text('synthetic')
        self.file.symlink_to(private)
        with self.assertRaises(CredentialError):
            self.store.read(self.target)
        self.file.unlink()
        self.file.write_text('invalid-json')
        self.file.chmod(0o600)
        with self.assertRaises(CredentialError):
            self.store.read(self.target)

    def test_write_sends_secret_on_stdin_and_persists_only_ciphertext(self) -> None:
        self.store.encrypted_path.parent.mkdir()
        with patch.object(self.store, '_root_directory'), patch('ogame_bot.linux_credentials.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=b'SYNTHETIC-CIPHERTEXT')) as encrypt:
            self.store.write(self.target, SECRET)
        self.assertEqual(self.store.encrypted_path.read_bytes(), b'SYNTHETIC-CIPHERTEXT')
        self.assertNotIn(SECRET.password, ' '.join(encrypt.call_args.args[0]))
        self.assertIn(SECRET.password.encode(), encrypt.call_args.kwargs['input'])
        self.assertEqual(self.store.encrypted_path.stat().st_mode & 0o777, 0o600)

    def test_failed_encryption_never_publishes_plaintext(self) -> None:
        self.store.encrypted_path.parent.mkdir()
        with patch.object(self.store, '_root_directory'), patch('ogame_bot.linux_credentials.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout=b'')):
            with self.assertRaises(CredentialError):
                self.store.write(self.target, SECRET)
        self.assertFalse(self.store.encrypted_path.exists())


@unittest.skipUnless(os.name == 'posix' and os.environ.get('OGAME_SYSTEMD_TESTS') == '1', 'Native systemd encryption test is opt-in and requires root')
class NativeSystemdCredentialTests(unittest.TestCase):
    def test_native_encrypt_decrypt_and_bound_runtime_read(self) -> None:
        self.assertEqual(os.geteuid(), 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(ROOT / 'config.example.yaml')
            target = credential_target(config)
            store = SystemdCredentialStore(root / 'sealed' / CREDENTIAL_ID)
            store.write(target, SECRET)
            self.assertNotIn(SECRET.password.encode(), store.encrypted_path.read_bytes())
            result = subprocess.run(['/usr/bin/systemd-creds', '--name='+CREDENTIAL_ID, 'decrypt', str(store.encrypted_path), '-'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
            credentials = root / 'runtime'
            credentials.mkdir(mode=0o700)
            file = credentials / CREDENTIAL_ID
            file.write_bytes(result.stdout)
            file.chmod(0o600)
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(credentials)}):
                self.assertEqual(store.read(target), SECRET)
            store.delete(target)
            self.assertFalse(store.encrypted_path.exists())


class DeploymentContractTests(unittest.TestCase):
    def test_services_use_unprivileged_panel_and_named_encrypted_credentials(self) -> None:
        service = (ROOT / 'deploy/linux/ogame.service').read_text()
        self.assertIn('User=ogame', service)
        self.assertIn('LoadCredentialEncrypted=ogame-lobby', service)
        self.assertIn('KillMode=mixed', service)
        display = (ROOT / 'deploy/linux/start_display.sh').read_text()
        self.assertIn('-nolisten tcp', display)
        self.assertIn('-auth', display)
        self.assertNotIn(' -ac', display)
        viewer = (ROOT / 'deploy/linux/view_browser.sh').read_text()
        self.assertIn('-listen 127.0.0.1', viewer)
        self.assertIn('-rfbauth', viewer)
