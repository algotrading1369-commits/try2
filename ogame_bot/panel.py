"""Servidor HTTP del panel, enlazado exclusivamente a loopback."""
from __future__ import annotations

import argparse
import hmac
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .errors import CoreError
from .panel_service import PanelError, PanelService

ASSETS = Path(__file__).with_name("panel_assets")


def allowed_request(host: str, origin: str | None, port: int, *, supplied_token: str | None = None, token: str | None = None) -> bool:
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}"} or (origin is not None and origin not in origins):
        return False
    if token is not None:
        return origin in origins and supplied_token is not None and hmac.compare_digest(supplied_token, token)
    return True


def handler_for(service: PanelService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def json_response(self, status: int, data: Any) -> None:
            self.respond(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if not allowed_request(self.headers.get("Host", ""), self.headers.get("Origin"), self.server.server_port):
                self.json_response(403, {"error": "Acceso limitado al panel local."})
                return
            try:
                if self.path == "/api/state":
                    self.json_response(200, service.state())
                    return
                files = {"/": ("index.html", "text/html"), "/panel.css": ("panel.css", "text/css"), "/panel.js": ("panel.js", "text/javascript")}
                if self.path not in files:
                    self.json_response(404, {"error": "Ruta desconocida."})
                    return
                filename, mime = files[self.path]
                self.respond(200, (ASSETS / filename).read_bytes(), mime + "; charset=utf-8")
            except (OSError, ValueError, CoreError):
                self.json_response(500, {"error": "No se pudo leer el estado local. Revisa configuración y registros."})

        def do_POST(self) -> None:
            if not allowed_request(self.headers.get("Host", ""), self.headers.get("Origin"), self.server.server_port,
                                   supplied_token=self.headers.get("X-Panel-Token"), token=service.csrf):
                self.json_response(403, {"error": "Petición no autorizada. Recarga el panel local."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get_content_type() != "application/json":
                    raise PanelError("Se requiere una petición JSON de tamaño limitado.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise PanelError("Formato de petición inválido.")
                if self.path == "/api/settings":
                    result = service.save_settings(payload)
                elif self.path == "/api/start" and not payload:
                    result = service.start()
                elif self.path == "/api/stop" and not payload:
                    result = service.stop()
                else:
                    raise PanelError("Acción desconocida.", 404)
                self.json_response(200, result)
            except PanelError as error:
                self.json_response(error.status, {"error": str(error)})
            except CoreError as error:
                self.json_response(400, {"error": str(error)})
            except (ValueError, TypeError, KeyError, OSError):
                self.json_response(400, {"error": "No se pudo aplicar la petición; la configuración debe ser válida."})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Panel local de OGame")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yaml")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("El puerto debe estar entre 1024 y 65535.")
    try:
        service = PanelService(args.config)
        url = f"http://127.0.0.1:{args.port}"
        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(service))
        except OSError:
            try:
                with urlopen(url + "/api/state", timeout=2) as response:
                    existing = json.load(response)
                if existing.get("application") == "ogame-local-panel" and existing.get("installation") == service.installation:
                    if args.open:
                        webbrowser.open(url)
                    print("El panel ya está abierto: " + url)
                    return 0
            except (OSError, ValueError, URLError):
                pass
            print("Puerto ocupado. Usa --port con otro puerto local.")
            return 1
        if args.open:
            webbrowser.open(url)
        print("Panel local: " + url + ". Mantén este servicio abierto mientras lo utilices.")
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print("Panel cerrado. El bot, si está activo, conserva su propio proceso.")
        finally:
            server.server_close()
        return 0
    except (CoreError, OSError):
        print("No se pudo abrir el panel: comprueba la configuración y las dependencias.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
