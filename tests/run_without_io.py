"""Runner de lógica para entornos Windows que bloquean el socketpair de asyncio.

No sirve para pruebas de navegador, red, procesos ni trabajo entre hilos.
No se importa desde el código de producción.
"""
from __future__ import annotations

import asyncio
import os
import selectors
import sys
import time
import unittest
from pathlib import Path
from typing import Any


class NoIOSelector(selectors.BaseSelector):
    def __init__(self) -> None:
        self.mapping: dict[Any, selectors.SelectorKey] = {}

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        key = selectors.SelectorKey(fileobj, fileobj, events, data)
        self.mapping[fileobj] = key
        return key

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self.mapping.pop(fileobj)

    def select(self, timeout: float | None = None) -> list[Any]:
        if timeout:
            time.sleep(timeout)
        return []

    def get_map(self) -> dict[Any, selectors.SelectorKey]:
        return self.mapping


class LogicLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__(NoIOSelector())

    def _make_self_pipe(self) -> None:
        return None

    def _close_self_pipe(self) -> None:
        return None

    def _write_to_self(self) -> None:
        return None


class LogicPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self) -> LogicLoop:
        return LogicLoop()


def main() -> int:
    if os.environ.get("OGAME_BROWSER_TESTS") == "1":
        print("Este runner no admite pruebas con navegador. Usa unittest normal.")
        return 1
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    asyncio.set_event_loop_policy(LogicPolicy())
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    return int(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
