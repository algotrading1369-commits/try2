"""Opt-in Lobby credentials in the current Windows user's Credential Manager.

No enumeration, plaintext fallback, command-line password, or secret logging.
Win32 contract: https://learn.microsoft.com/windows/win32/api/wincred/
"""
from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import json
import os
import sys
import tempfile
import warnings
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


class CredentialError(Exception):
    """Sanitized local credential-store failure."""


@dataclass(frozen=True)
class LoginCredentials:
    email: str = field(repr=False)
    password: str = field(repr=False)

    def validate(self) -> None:
        if not self.email or '@' not in self.email or any(c in self.email for c in '\r\n\x00') or not self.password or '\x00' in self.password:
            raise CredentialError('Introduce un correo y una contraseña válidos.')


def credential_target(config: Config) -> str:
    identity = config.server_origin + '|' + config.expected_player_name
    return 'OrbitOGame/Lobby/v1/' + hashlib.sha256(identity.encode('utf-8')).hexdigest()


class _Credential(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR),
    ]


def _windows_api() -> Any:
    if os.name != 'nt':
        raise CredentialError('El acceso automático requiere el Administrador de credenciales de Windows.')
    try:
        api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        return api
    except (OSError, AttributeError):
        raise CredentialError('Windows no permite acceder al almacén de credenciales.') from None


class WindowsCredentialStore:
    def __init__(self, api: Any | None = None) -> None:
        self.api = api if api is not None else _windows_api()

    @staticmethod
    def _target(target: str) -> None:
        if not target.startswith('OrbitOGame/Lobby/v1/') or not 1 <= len(target) <= 256 or '\x00' in target:
            raise CredentialError('Identificador de credencial no válido.')

    def write(self, target: str, credentials: LoginCredentials) -> None:
        self._target(target)
        credentials.validate()
        try:
            payload = json.dumps({'version': 1, 'email': credentials.email, 'password': credentials.password}, ensure_ascii=False).encode('utf-8')
        except UnicodeError:
            raise CredentialError('Las credenciales contienen caracteres no admitidos.') from None
        if len(payload) > 2560:
            raise CredentialError('Las credenciales superan el tamaño admitido por Windows.')
        buffer = ctypes.create_string_buffer(payload, len(payload))
        credential = _Credential()
        credential.Type = 1  # CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = 'ORBIT: acceso automático al Lobby de OGame'
        credential.CredentialBlobSize = len(payload)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE: current user, this PC
        credential.UserName = 'OGame Lobby'
        try:
            if not self.api.CredWriteW(ctypes.byref(credential), 0):
                if ctypes.get_last_error() in {5, 1312}:
                    raise CredentialError('Windows denegó el acceso al almacén. Ejecuta Configurar_acceso.bat desde tu sesión normal de Windows con el usuario que ejecuta el bot.')
                raise CredentialError('No se pudieron guardar las credenciales en Windows.')
        except OSError:
            raise CredentialError('No se pudieron guardar las credenciales en Windows.') from None
        finally:
            ctypes.memset(buffer, 0, len(payload))

    def read(self, target: str) -> LoginCredentials | None:
        self._target(target)
        pointer = ctypes.POINTER(_Credential)()
        try:
            if not self.api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
                if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
                    return None
                raise CredentialError('No se pudieron leer las credenciales de Windows.')
            try:
                value = pointer.contents
                if not value.CredentialBlob or not 1 <= value.CredentialBlobSize <= 2560:
                    raise CredentialError('La credencial de Windows no tiene un formato válido.')
                raw = json.loads(ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode('utf-8'))
                if not isinstance(raw, dict) or raw.get('version') != 1 or type(raw.get('email')) is not str or type(raw.get('password')) is not str:
                    raise CredentialError('La credencial de Windows no tiene un formato válido.')
                result = LoginCredentials(raw['email'], raw['password'])
                result.validate()
                return result
            finally:
                self.api.CredFree(pointer)
        except (OSError, ValueError, UnicodeError):
            raise CredentialError('No se pudieron leer las credenciales de Windows.') from None

    def delete(self, target: str) -> None:
        self._target(target)
        try:
            if not self.api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
                raise CredentialError('No se pudo retirar la credencial de Windows.')
        except OSError:
            raise CredentialError('No se pudo retirar la credencial de Windows.') from None


def set_auto_login(path: Path, enabled: bool) -> None:
    import yaml
    from .config import load_config
    load_config(path)
    temporary: str | None = None
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        raw['session']['auto_login'] = enabled
        raw['session'].setdefault('login_retry_seconds', 300)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.resolve().parent, suffix='.yaml', delete=False) as output:
            temporary = output.name
            yaml.safe_dump(raw, output, allow_unicode=True, sort_keys=False)
        load_config(Path(temporary))
        os.replace(temporary, path)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CredentialError('No se pudo actualizar la configuración local.') from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def main() -> int:
    from .config import load_config
    from .errors import CoreError
    parser = argparse.ArgumentParser(description='Acceso al Lobby guardado exclusivamente en Windows.')
    parser.add_argument('command', choices=('configure', 'status', 'remove'))
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.yaml')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        store = WindowsCredentialStore()
        target = credential_target(config)
        if args.command == 'status':
            present = store.read(target) is not None
            print('Credenciales guardadas: ' + ('sí' if present else 'no'))
            print('Acceso automático activado: ' + ('sí' if config.auto_login else 'no'))
        elif args.command == 'remove':
            set_auto_login(args.config, False)
            store.delete(target)
            print('Acceso automático desactivado y credencial retirada de Windows.')
        else:
            if not sys.stdin.isatty():
                raise CredentialError('Abre Configurar_acceso.bat en una consola local interactiva.')
            print('Estas credenciales solo se usarán para iniciar sesión en el Lobby oficial de OGame.')
            print('Se guardarán en el Administrador de credenciales de tu usuario de Windows.')
            email = input('Correo de Gameforge: ').strip()
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                password = getpass.getpass('Contraseña (no se muestra): ')
                confirmation = getpass.getpass('Repite la contraseña: ')
            if password != confirmation:
                raise CredentialError('Las contraseñas no coinciden; no se ha guardado el acceso.')
            store.write(target, LoginCredentials(email, password))
            set_auto_login(args.config, True)
            print('Acceso automático configurado. Reinicia el bot para activarlo.')
    except (CredentialError, CoreError) as error:
        print(str(error))
        return 1
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print('Configuración cancelada; no se ha completado el alta.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
