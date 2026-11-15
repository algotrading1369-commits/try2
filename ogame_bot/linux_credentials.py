"""Encrypted-at-rest systemd service credential, scoped to one configured account.

The root CLI encrypts via stdin. The unprivileged service reads only its named
read-only credential supplied by systemd, never the host encryption key.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from .credentials import CredentialError, LoginCredentials, WindowsCredentialStore

CREDENTIAL_ID = 'ogame-lobby'
ENCRYPTED_PATH = Path('/etc/credstore.encrypted') / CREDENTIAL_ID


class SystemdCredentialStore:
    def __init__(self, encrypted_path: Path = ENCRYPTED_PATH) -> None:
        self.encrypted_path = encrypted_path

    def read(self, target: str) -> LoginCredentials | None:
        WindowsCredentialStore._target(target)
        directory = os.environ.get('CREDENTIALS_DIRECTORY')
        if not directory:
            return None
        parent = Path(directory)
        if not parent.is_absolute() or parent.is_symlink():
            raise CredentialError('Directorio de credenciales del servicio no válido.')
        descriptor: int | None = None
        try:
            descriptor = os.open(parent / CREDENTIAL_ID, os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not 0 < info.st_size <= 16384:
                raise CredentialError('La credencial del servicio no tiene permisos o formato válidos.')
            with os.fdopen(descriptor, 'rb') as handle:
                descriptor = None
                raw = json.loads(handle.read(16385).decode('utf-8'))
            if not isinstance(raw, dict) or raw.get('version') != 1 or raw.get('target') != target:
                raise CredentialError('La credencial del servicio pertenece a otra cuenta o no es válida.')
            if type(raw.get('email')) is not str or type(raw.get('password')) is not str:
                raise CredentialError('Formato de credencial del servicio no válido.')
            result = LoginCredentials(raw['email'], raw['password'])
            result.validate()
            return result
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError):
            raise CredentialError('No se pudo leer la credencial del servicio.') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _root_directory(self) -> None:
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise CredentialError('Configura el acceso Linux con sudo desde una consola SSH interactiva.')
        parent = self.encrypted_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or self.encrypted_path.is_symlink():
            raise CredentialError('El almacén cifrado debe estar bajo control exclusivo de root.')

    def write(self, target: str, credentials: LoginCredentials) -> None:
        WindowsCredentialStore._target(target)
        credentials.validate()
        temporary: str | None = None
        try:
            self._root_directory()
            payload = json.dumps({'version': 1, 'target': target, 'email': credentials.email, 'password': credentials.password}, ensure_ascii=False).encode('utf-8')
            if len(payload) > 16384:
                raise CredentialError('Las credenciales superan el tamaño admitido.')
            result = subprocess.run(['/usr/bin/systemd-creds', '--name='+CREDENTIAL_ID, '--with-key=host', 'encrypt', '-', '-'],
                input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
            if result.returncode or not result.stdout or len(result.stdout) > 65536:
                raise CredentialError('systemd no pudo cifrar las credenciales; no se guardó texto plano.')
            with tempfile.NamedTemporaryFile(dir=self.encrypted_path.parent, prefix='.ogame-', delete=False) as output:
                temporary = output.name
                os.fchmod(output.fileno(), 0o600)
                output.write(result.stdout)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.encrypted_path)
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise CredentialError('No se pudo guardar el acceso cifrado de systemd.') from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def delete(self, target: str) -> None:
        WindowsCredentialStore._target(target)
        try:
            self._root_directory()
            self.encrypted_path.unlink(missing_ok=True)
        except OSError:
            raise CredentialError('No se pudo retirar la credencial cifrada.') from None

    def configured(self, target: str) -> bool:
        WindowsCredentialStore._target(target)
        if os.environ.get('CREDENTIALS_DIRECTORY'):
            return self.read(target) is not None
        try:
            return self.encrypted_path.is_file() and not self.encrypted_path.is_symlink()
        except OSError:
            raise CredentialError('No se pudo comprobar el almacén cifrado.') from None
