"""Registro duradero y exclusivo de órdenes previo al clic."""
from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from ..brain.planner import Upgrade
from ..domain.models import utc_now
from ..errors import CoreError, JournalAccountMismatch
from ..fleet.models import Flight

ACTIVE = ("prepared", "submitted", "uncertain")
logger = logging.getLogger(__name__)


class OrderJournal:
    @classmethod
    def open_for_account(cls, path: Path, account: str) -> OrderJournal:
        """Reutiliza el registro heredado o un archivo estable de esta identidad.

        No cambia la cuenta vinculada ni elimina órdenes. Un registro heredado
        de otra identidad con órdenes inciertas requiere revisión explícita.
        """
        if path.exists():
            try:
                return cls(path, account)
            except JournalAccountMismatch:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as previous:
                    table = previous.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
                    if table is None:
                        raise CoreError("El registro anterior no tiene un formato verificable; se conserva sin modificar.") from None
                    active = previous.execute("SELECT 1 FROM orders WHERE state IN ('prepared', 'submitted', 'uncertain') LIMIT 1").fetchone()
                if active is not None:
                    raise CoreError("El registro anterior pertenece a otra cuenta o servidor y contiene órdenes sin confirmar. Se conserva: revisa esas órdenes antes de continuar.") from None
                logger.info("La configuración usa otra identidad; se conserva el registro anterior y se abre uno independiente.")
        digest = hashlib.sha256(account.encode("utf-8")).hexdigest()[:20]
        selected = path.with_name(path.stem + "-" + digest + (path.suffix or ".sqlite3"))
        return cls(selected, account)

    def __init__(self, path: Path, account: str) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = self.connection.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
            if row is not None and row[0] != account:
                raise JournalAccountMismatch("El registro de órdenes pertenece a otra cuenta o servidor.")
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('account', ?)", (account,))
            self.connection.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, state TEXT NOT NULL, action TEXT NOT NULL)")
            if "scope" not in {column[1] for column in self.connection.execute("PRAGMA table_info(orders)")}:
                self.connection.execute("ALTER TABLE orders ADD COLUMN scope TEXT NOT NULL DEFAULT 'upgrade'")
            self.connection.execute("DROP INDEX IF EXISTS one_active_order")
            # Older releases serialized all economic queues under 'upgrade'.
            # Preserve every receipt while separating the actual game queues.
            for order in self.connection.execute("SELECT id, action FROM orders WHERE scope='upgrade'").fetchall():
                try:
                    action = Upgrade.from_dict(json.loads(order["action"]))
                except (ValueError, TypeError, KeyError) as error:
                    raise CoreError("Invalid economic journal; review pending orders before continuing.") from error
                self.connection.execute("UPDATE orders SET scope=? WHERE id=?", (self.upgrade_scope(action), order["id"]))
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_scope ON orders (scope) WHERE state IN ('prepared', 'submitted', 'uncertain')")
            self.connection.commit()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def upgrade_scope(action: Upgrade) -> str:
        return "upgrade:research" if action.kind == "research" else f"upgrade:{action.kind}:{action.body_id}"

    def pending_upgrades(self) -> list[tuple[str, Upgrade]]:
        rows = self.connection.execute("SELECT id, action FROM orders WHERE state IN ('prepared', 'submitted', 'uncertain') AND scope LIKE 'upgrade:%' ORDER BY created_at, rowid")
        try:
            return [(row["id"], Upgrade.from_dict(json.loads(row["action"]))) for row in rows]
        except (ValueError, TypeError, KeyError) as error:
            raise CoreError("Invalid pending economic order; no new orders will be sent.") from error

    def pending_for(self, action: Upgrade) -> tuple[str, Upgrade] | None:
        scope = self.upgrade_scope(action)
        return next((order for order in self.pending_upgrades() if self.upgrade_scope(order[1]) == scope), None)

    def produced(self, key: str, body_id: int, item: str) -> int:
        total = 0
        for row in self.connection.execute("SELECT state, action FROM orders WHERE scope LIKE 'upgrade:shipyard:%'"):
            try:
                action = Upgrade.from_dict(json.loads(row["action"]))
            except (ValueError, TypeError, KeyError) as error:
                raise CoreError("No se pudo verificar el historial del hangar.") from error
            if action.production_key != key:
                continue
            if action.body_id != body_id or action.item != item:
                raise CoreError("El ID de fabricación ya pertenece a otra nave o planeta. Añade una orden nueva.")
            if row["state"] == "confirmed":
                total += action.quantity
        return total

    def pending(self, scope: str | None = None) -> tuple[str, Upgrade | Flight] | None:
        if scope not in {None, "upgrade", "fleet"}:
            raise ValueError("Categoría de orden inválida.")
        if scope == "upgrade":
            return next(iter(self.pending_upgrades()), None)
        row = self.connection.execute("SELECT id, action FROM orders WHERE state IN ('prepared', 'submitted', 'uncertain') AND (? IS NULL OR scope=?) ORDER BY created_at, rowid LIMIT 1", (scope, scope)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["action"])
            return row["id"], Flight.from_dict(data) if data.get("kind") == "fleet" else Upgrade.from_dict(data)
        except (ValueError, TypeError, KeyError) as error:
            raise CoreError("El registro pendiente es inválido; no se enviarán nuevas órdenes.") from error

    def prepare(self, upgrade: Upgrade | Flight) -> str:
        identifier = uuid.uuid4().hex
        now = utc_now()
        try:
            with self.connection:
                self.connection.execute("INSERT INTO orders (id, created_at, updated_at, state, action, scope) VALUES (?, ?, ?, 'prepared', ?, ?)", (identifier, now, now, json.dumps(upgrade.to_dict(), ensure_ascii=False), "fleet" if isinstance(upgrade, Flight) else self.upgrade_scope(upgrade)))
        except sqlite3.IntegrityError as error:
            raise CoreError("Ya existe una orden pendiente de reconciliar.") from error
        return identifier

    def mark(self, identifier: str, state: str) -> None:
        if state not in {"submitted", "uncertain", "confirmed"}:
            raise ValueError("Estado de orden inválido.")
        with self.connection:
            cursor = self.connection.execute("UPDATE orders SET state=?, updated_at=? WHERE id=? AND state IN ('prepared', 'submitted', 'uncertain')", (state, utc_now(), identifier))
            if cursor.rowcount != 1:
                if state == "confirmed":
                    existing = self.connection.execute("SELECT state FROM orders WHERE id=?", (identifier,)).fetchone()
                    if existing is not None and existing[0] == "confirmed":
                        return
                raise CoreError("No se pudo actualizar la orden pendiente.")

    def last_completed_kind(self) -> str | None:
        row = self.connection.execute("SELECT action FROM orders WHERE state='confirmed' AND scope LIKE 'upgrade:%' ORDER BY updated_at DESC, rowid DESC LIMIT 1").fetchone()
        return json.loads(row[0])["kind"] if row is not None else None

    def probe_completed(self, body_id: int) -> bool:
        rows = self.connection.execute("SELECT action FROM orders WHERE state='confirmed' AND scope LIKE 'upgrade:%' ORDER BY updated_at DESC, rowid DESC")
        try:
            for row in rows:
                action = json.loads(row[0])
                if action.get("kind") == "shipyard" and action.get("item") == "espionage_probe" and action.get("body_id") == body_id:
                    return True
        except (ValueError, TypeError, AttributeError) as error:
            raise CoreError("No se pudo verificar el historial de fabricación; no se enviarán nuevas órdenes.") from error
        return False
