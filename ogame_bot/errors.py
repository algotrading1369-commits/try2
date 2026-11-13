"""Errores públicos del núcleo."""


class CoreError(Exception):
    """Error recuperable o comunicado al operador sin datos de sesión."""


class JournalAccountMismatch(CoreError):
    """El registro existente está vinculado a otra identidad configurada."""


class ConfigurationError(CoreError):
    """Configuración inválida."""


class IdentityMismatch(CoreError):
    """El servidor o la identidad no corresponden a la cuenta esperada."""


class LoginRequired(CoreError):
    """Se requiere intervención en el navegador."""


class BrowserClosed(CoreError):
    """El operador cerró el navegador o su proceso terminó."""


class ConnectionLost(CoreError):
    """La reconexión agotó sus intentos."""


class ReadError(CoreError):
    """La interfaz no permite una lectura fiable de la sección solicitada."""
