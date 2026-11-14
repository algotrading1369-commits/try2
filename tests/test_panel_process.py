"""Real subprocess stop handshake without opening a game or browser."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ogame_bot.panel_service import PanelService

RECEIVER = """
import sys,time
from pathlib import Path
from ogame_bot.core.panel_control import register_control,unregister_control,panel_stop_requested
directory=Path(sys.argv[1])
token=register_control(directory)
try:
    deadline=time.monotonic()+10
    while not panel_stop_requested(directory/'panel_stop.json'):
        if time.monotonic()>deadline:
            raise RuntimeError('Stop deadline exceeded')
        time.sleep(0.05)
finally:
    unregister_control(directory,token)
"""


class ProcessControlTests(unittest.TestCase):
    def test_reopened_panel_stops_registered_receiver_without_os_signal(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = folder / "config.yaml"
            config.write_bytes((root / "config.example.yaml").read_bytes())
            service = PanelService(config)
            directory = service.config.scheduler.status_file.parent
            child = subprocess.Popen([sys.executable, "-c", RECEIVER, str(directory)], cwd=root,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not service.process_status()["can_stop"]:
                    if child.poll() is not None or time.monotonic() > deadline:
                        self.fail("The isolated receiver did not register control.")
                    time.sleep(0.05)
                reopened = PanelService(config)
                reopened.stop()
                child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0)
                self.assertFalse(reopened.process_status()["can_stop"])
                self.assertFalse((directory / "bot_control.json").exists())
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)
